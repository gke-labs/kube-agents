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

"""The agent-pod read and the ``bootstrap_report_read`` verifier.

The marker and report paths are pointed into a temporary directory and both
shells run the read command under ``sh``, so each verdict comes from the files
as the command sees them.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from devops_bench.verification.base import VERIFIERS
from devops_bench.verification.spec import parse_node

from kube_agents_bench import onboarding
from kube_agents_bench.verifiers import BootstrapReportReadVerifier

REPO = Path(__file__).resolve().parents[2]
TASK = REPO / "bench" / "tasks" / "bootstrap-inventory-ranking-delivery" / "task.yaml"
STACK = REPO / "bench" / "tf" / "prebuilt" / "bootstrap-ranking" / "main.tf"
DELIVERY = REPO / "agents" / "chat" / "scripts" / "bootstrap_delivery.py"


def _objective() -> dict[str, Any]:
    spec = yaml.safe_load(TASK.read_text().split("\n---\n", 1)[1])["verification_spec"]
    return next(e for e in spec if e["name"] == "report-read-from-sandbox")["check"]


def _local_shell(script: str, timeout: float) -> str:
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=timeout, check=True).stdout


@pytest.fixture
def pods(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One directory standing in for both pods' data volumes, read locally."""
    monkeypatch.setattr(onboarding, "COMPLETED_MARKER", str(tmp_path / ".bootstrap_completed"))
    monkeypatch.setattr(onboarding, "REPORT_FILE", str(tmp_path / "INVENTORY.md"))
    monkeypatch.setattr(onboarding, "DELIVERED_FILE", str(tmp_path / "INVENTORY.delivered.md"))
    monkeypatch.setattr(onboarding, "agent_shell", _local_shell)
    monkeypatch.setattr(onboarding, "sandbox_shell", _local_shell)
    return tmp_path


def _plant(pods: Path, *names: str) -> None:
    for name in names:
        (pods / name).write_text("x")


def _verify(timeout: float = 0.0):
    return BootstrapReportReadVerifier(type="bootstrap_report_read").verify(timeout)


# --- the paths ------------------------------------------------------------


def test_the_paths_are_the_ones_the_delivery_script_writes() -> None:
    script = DELIVERY.read_text()
    assert 'SANDBOX_HOME = "/opt/data"' in script
    assert f'REPORT_NAME = "{Path(onboarding.REPORT_FILE).name}"' in script
    assert f'DELIVERED_REPORT_NAME = "{Path(onboarding.DELIVERED_FILE).name}"' in script
    assert '".bootstrap_completed"' in script
    for path in (onboarding.COMPLETED_MARKER, onboarding.REPORT_FILE, onboarding.DELIVERED_FILE):
        assert str(Path(path).parent) == "/opt/data"


# --- the reads ------------------------------------------------------------


def test_read_files_reports_each_path(tmp_path: Path) -> None:
    there, missing = tmp_path / "there", tmp_path / "missing file"
    there.write_text("x")
    assert onboarding.read_files(_local_shell, [str(there), str(missing)], 5.0) == {
        str(there): True,
        str(missing): False,
    }


@pytest.mark.parametrize(
    "reply",
    ["", "present /a\n", "present /a\n__ONBOARDING_FILES_READ__\n", "error: no such container\n"],
)
def test_a_reply_without_the_sentinel_or_a_path_is_a_failed_read(reply: str) -> None:
    assert onboarding.read_files(lambda s, t: reply, ["/a", "/b"], 5.0) is None


def test_agent_shell_execs_into_the_agent_service(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def run(cmd, **kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="out", stderr="")

    monkeypatch.setattr(onboarding.subprocess, "run", run)
    monkeypatch.setenv("AGENT_SERVICE_NAME", "agent-x")
    monkeypatch.setenv("AGENT_NAMESPACE", "ns-x")
    monkeypatch.setenv("AGENT_CLUSTER_CONTEXT", "ctx-x")
    monkeypatch.delenv("AGENT_CONTAINER", raising=False)
    assert onboarding.agent_shell("echo hi", 5.0) == "out"
    assert seen[-1] == [
        "kubectl", "exec", "svc/agent-x", "-n", "ns-x", "--context", "ctx-x",
        "-c", "platform-agent", "--", "sh", "-c", "echo hi",
    ]
    monkeypatch.setenv("AGENT_CONTAINER", "other")
    onboarding.agent_shell("echo hi", 5.0)
    assert seen[-1][7:9] == ["-c", "other"]


def test_agent_shell_returns_nothing_on_a_failed_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        onboarding.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="partial", stderr="x")
    )
    assert onboarding.agent_shell("true", 5.0) == ""


# --- the objective --------------------------------------------------------


def test_a_claimed_and_archived_report_passes(pods: Path) -> None:
    _plant(pods, ".bootstrap_completed", "INVENTORY.delivered.md")
    result = _verify()
    assert result.status == "pass", result.reason
    assert result.raw == {"claimed": True, "report": False, "delivered": True}


def test_a_report_left_on_the_sandbox_unclaimed_fails(pods: Path) -> None:
    _plant(pods, "INVENTORY.md")
    result = _verify()
    assert result.status == "fail"
    assert "did not read the report off the sandbox" in result.reason


def test_a_dangling_report_symlink_reads_as_a_report_delivery_did_not_take(pods: Path) -> None:
    (pods / "INVENTORY.md").symlink_to(pods / "gone")
    result = _verify()
    assert result.raw == {"claimed": False, "report": True, "delivered": False}
    assert "did not read the report off the sandbox" in result.reason


def test_a_dangling_marker_reads_as_unclaimed(pods: Path) -> None:
    _plant(pods, "INVENTORY.md")
    (pods / ".bootstrap_completed").symlink_to(pods / "gone")
    result = _verify()
    assert result.raw == {"claimed": False, "report": True, "delivered": False}


def test_no_report_at_all_fails_as_nothing_to_deliver(pods: Path) -> None:
    result = _verify()
    assert result.status == "fail"
    assert "wrote no report" in result.reason


def test_an_archived_report_without_the_marker_fails(pods: Path) -> None:
    _plant(pods, "INVENTORY.delivered.md")
    result = _verify()
    assert result.status == "fail"
    assert "holds INVENTORY.delivered.md but there is no" in result.reason


def test_a_claimed_report_left_in_place_fails(pods: Path) -> None:
    _plant(pods, ".bootstrap_completed", "INVENTORY.md", "INVENTORY.delivered.md")
    result = _verify()
    assert result.status == "fail"
    assert "did not archive" in result.reason


def test_a_claim_with_no_report_either_way_fails(pods: Path) -> None:
    _plant(pods, ".bootstrap_completed")
    result = _verify()
    assert result.status == "fail"
    assert "holds neither" in result.reason


def test_an_unreadable_agent_pod_is_an_error(pods: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onboarding, "agent_shell", lambda s, t: "")
    result = _verify()
    assert result.status == "error"
    assert "agent pod" in result.reason


def test_an_unreadable_sandbox_is_an_error(pods: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onboarding, "sandbox_shell", lambda s, t: "")
    assert _verify().status == "error"


def test_the_claim_is_waited_for(pods: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _plant(pods, "INVENTORY.md")
    reads = {"n": 0}

    def delivers_on_the_second_read(script: str, timeout: float) -> str:
        reads["n"] += 1
        if reads["n"] == 2:
            (pods / "INVENTORY.md").rename(pods / "INVENTORY.delivered.md")
            _plant(pods, ".bootstrap_completed")
        return _local_shell(script, timeout)

    monkeypatch.setattr(onboarding, "agent_shell", delivers_on_the_second_read)
    result = _verify(timeout=5.0)
    assert result.status == "pass", result.reason


@pytest.mark.parametrize("after", [1, 2], ids=["before-the-agent-read", "after-the-agent-read"])
@pytest.mark.parametrize("planted", [("INVENTORY.md",), ()], ids=["report-then-delivered", "written-and-delivered"])
def test_a_delivery_between_the_reads_reads_as_the_delivered_state(
    pods: Path, monkeypatch: pytest.MonkeyPatch, planted: tuple[str, ...], after: int
) -> None:
    _plant(pods, *planted)
    reads: list[str] = []

    def delivers_after_a_read(script: str, timeout: float) -> str:
        out = _local_shell(script, timeout)
        reads.append(script)
        if len(reads) == after:
            (pods / "INVENTORY.md").unlink(missing_ok=True)
            _plant(pods, ".bootstrap_completed", "INVENTORY.delivered.md")
        return out

    monkeypatch.setattr(onboarding, "agent_shell", delivers_after_a_read)
    monkeypatch.setattr(onboarding, "sandbox_shell", delivers_after_a_read)
    result = _verify()
    assert (result.status, result.raw) == ("pass", {"claimed": True, "report": False, "delivered": True}), result.reason


def test_a_sandbox_that_fails_its_second_read_is_an_error(pods: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list[str] = []

    def answers_once(script: str, timeout: float) -> str:
        reads.append(script)
        return _local_shell(script, timeout) if len(reads) == 1 else ""

    monkeypatch.setattr(onboarding, "sandbox_shell", answers_once)
    result = _verify()
    assert result.status == "error"
    assert onboarding.sandbox_pod() in result.reason


# --- registration and the stack --------------------------------------------


def test_the_verifier_is_published_as_an_entry_point() -> None:
    with (REPO / "bench" / "pyproject.toml").open("rb") as fh:
        eps = tomllib.load(fh)["project"]["entry-points"]["devops_bench.verifiers"]
    assert eps["bootstrap_report_read"] == "kube_agents_bench.verifiers:BootstrapReportReadVerifier"


def test_parse_node_builds_the_case_objective() -> None:
    assert isinstance(parse_node(_objective()), BootstrapReportReadVerifier)
    assert VERIFIERS.get("bootstrap_report_read") is BootstrapReportReadVerifier


def test_the_stack_arms_and_disarms_the_jobs_the_delivery_script_names() -> None:
    script, stack = DELIVERY.read_text(), STACK.read_text()
    assert 'SCAN_JOB_ID = "bootstrap-inventory-scan"' in script
    assert 'DELIVERY_JOB_ID = "bootstrap-inventory-delivery"' in script
    assert 'scan_job     = "bootstrap-inventory-scan"' in stack
    assert 'delivery_job = "bootstrap-inventory-delivery"' in stack
    # Arming is the last step of the create, and the destroy undoes it.
    create, destroy = stack.split("when        = destroy", 1)
    assert create.rindex("base64encode(local.arm_py)") > create.index("# ---- 5.")
    assert "self.triggers.disarm_b64" in destroy
