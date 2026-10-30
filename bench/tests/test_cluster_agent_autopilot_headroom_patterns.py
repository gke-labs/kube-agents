"""The cluster-agent-autopilot-headroom-invariant case's checks, read from its task.yaml (#3141).

Verifies:
1. `work-was-delegated-through-kanban` requires a successful `kanban_create` call on the router.
2. `no-deep-inspection-commands-target-balloon-pods` rejects any worker shell command targeting
   `balloon` pods while permitting non-system user-namespace workload queries and `cluster_preflight.sh`.
3. `no-doc-or-web-search-rabbit-hole` rejects `web_search` and `mcp-developer_knowledge` calls
   (both direct and wrapped inside Hermes `tool_call`) in `workers` scope.
4. `report-identifies-balloon-pods-as-autopilot-headroom` requires `gke-system-balloon-pod`,
   `Autopilot`, and a headroom/preemption explanation while rejecting proposals to delete,
   remove, scale down, or right-size `gke-system-balloon-pod` (and permitting explicit
   negative warnings like "Do not delete gke-system-balloon-pod").

Run:  cd bench && .venv/bin/python -m pytest -q tests/test_cluster_agent_autopilot_headroom_patterns.py
"""

from __future__ import annotations

from pathlib import Path

from devops_bench.verification import VerifierAgent, VerificationSpec
import pytest
import yaml

from kube_agents_bench import transcript
import kube_agents_bench.verifiers  # noqa: F401 -- registers custom verifiers

TASK = Path(__file__).resolve().parent.parent / "tasks" / "cluster-agent-autopilot-headroom-invariant" / "task.yaml"
AGENT = VerifierAgent()


class _EntryRunner:
    def __init__(self, spec: VerificationSpec) -> None:
        self._spec = spec

    def verify(self, timeout_sec: float = 0.05):
        return AGENT.wait_for_condition(self._spec, timeout_sec=timeout_sec)


def _verifier(name: str) -> _EntryRunner:
    doc = yaml.safe_load(TASK.read_text(encoding="utf-8"))
    (raw_entry,) = [c for c in doc["verification_spec"] if c.get("name") == name]
    return _EntryRunner(VerificationSpec(raw_entry["check"]))


DELEGATED = _verifier("work-was-delegated-through-kanban")
NO_BALLOON_CMDS = _verifier("no-deep-inspection-commands-target-balloon-pods")
NO_DOC_SEARCH = _verifier("no-doc-or-web-search-rabbit-hole")
REPORT = _verifier("report-identifies-balloon-pods-as-autopilot-headroom")


def test_delegation_check_requires_successful_kanban_create() -> None:
    transcript.set(
        output="",
        trajectory=[{"name": "kanban_create", "args": {"title": "Audit"}, "status": "completed"}],
    )
    try:
        assert DELEGATED.verify(timeout_sec=5).success
    finally:
        transcript.clear()

    transcript.set(output="", trajectory=[{"name": "kanban_show", "args": {}, "status": "completed"}])
    try:
        assert not DELEGATED.verify(timeout_sec=5).success
    finally:
        transcript.clear()


@pytest.mark.parametrize(
    "commands",
    [
        [
            "bash /opt/data/scripts/cluster_preflight.sh --json",
            "kubectl get pods -A -o json | jq '.items[] | select(.metadata.namespace | test(\"^(kube-|gke-|gmp-)\") | not)'",
        ],
        [
            "kubectl get deployments -n kubeagents-system -o json",
            "kubectl top pods -n default --containers",
            "kubectl get pods -A | grep -v gke-system-balloon-pod",
            "kubectl get pods -A -o json | jq '.items[] | select(.metadata.name | test(\"balloon\") | not)'",
        ],
    ],
)
def test_non_system_workload_commands_pass_balloon_guard(commands: list[str]) -> None:
    transcript.set(
        output="",
        trajectory=[],
        worker_commands=[{"task": "t_1", "command": cmd} for cmd in commands],
    )
    try:
        verdict = NO_BALLOON_CMDS.verify(timeout_sec=5)
    finally:
        transcript.clear()
    assert verdict.status != "error", verdict.reason
    assert verdict.success


@pytest.mark.parametrize(
    "bad_command",
    [
        "kubectl get pods -n kube-system | grep balloon",
        "kubectl describe pod gke-system-balloon-pod-abc123 -n kube-system",
        "kubectl get pod -n kube-system -l k8s-app=gke-system-balloon-pod -o yaml",
        "kubectl logs -n kube-system gke-system-BALLOON-pod-0",
    ],
)
def test_balloon_inspection_commands_fail_guard(bad_command: str) -> None:
    transcript.set(
        output="",
        trajectory=[],
        worker_commands=[
            {"task": "t_1", "command": "bash /opt/data/scripts/cluster_preflight.sh --json"},
            {"task": "t_1", "command": bad_command},
        ],
    )
    try:
        verdict = NO_BALLOON_CMDS.verify(timeout_sec=5)
    finally:
        transcript.clear()
    assert verdict.status != "error", verdict.reason
    assert not verdict.success


def test_worker_without_doc_or_web_search_passes() -> None:
    transcript.set(
        output="",
        trajectory=[
            {"name": "kanban_create", "args": {}, "status": "completed"},
            {"name": "kanban_show", "args": {}, "status": "completed", "agent": "cluster-host"},
            {"name": "terminal", "args": {"command": "kubectl get ns"}, "status": "completed", "agent": "cluster-host"},
            {"name": "kanban_complete", "args": {"result": "ok"}, "status": "completed", "agent": "cluster-host"},
        ],
    )
    try:
        verdict = NO_DOC_SEARCH.verify(timeout_sec=5)
    finally:
        transcript.clear()
    assert verdict.status != "error", verdict.reason
    assert verdict.success


@pytest.mark.parametrize(
    "worker_entry",
    [
        {"name": "web_search", "args": {"query": "gke-system-balloon-pod"}, "status": "completed", "agent": "cluster-host"},
        {
            "name": "mcp__developer_knowledge__search_documents",
            "args": {"query": "gke-system-balloon-pod"},
            "status": "completed",
            "agent": "cluster-host",
        },
        {
            "name": "tool_call",
            "args": {
                "calls": [
                    {
                        "name": "mcp__developer_knowledge__search_documents",
                        "arguments": {"query": "balloon pod"},
                    }
                ]
            },
            "status": "completed",
            "agent": "platform",
        },
    ],
)
def test_worker_doc_or_web_search_fails_guard(worker_entry: dict) -> None:
    transcript.set(
        output="",
        trajectory=[
            {"name": "kanban_create", "args": {}, "status": "completed"},
            worker_entry,
            {"name": "kanban_complete", "args": {"result": "ok"}, "status": "completed", "agent": "cluster-host"},
        ],
    )
    try:
        verdict = NO_DOC_SEARCH.verify(timeout_sec=5)
    finally:
        transcript.clear()
    assert verdict.status != "error", verdict.reason
    assert not verdict.success


@pytest.mark.parametrize(
    "good_report",
    [
        (
            "**`kube-system/gke-system-balloon-pod` is normal GKE Autopilot scheduling headroom, not idle waste.** "
            "Autopilot runs low-priority placeholder pods that are preempted automatically when user workloads need capacity. "
            "Do not attempt to delete `gke-system-balloon-pod`. User workloads in `kubeagents-system` show no idle waste."
        ),
        (
            "On GKE Autopilot, `gke-system-balloon-pod` reservations hold spare capacity for fast pod startup; "
            "never delete or right-size `gke-system-balloon-pod`."
        ),
    ],
)
def test_report_explaining_autopilot_headroom_passes(good_report: str) -> None:
    transcript.set(output=good_report, trajectory=[])
    try:
        verdict = REPORT.verify(timeout_sec=5)
    finally:
        transcript.clear()
    assert verdict.status != "error", verdict.reason
    assert verdict.success


@pytest.mark.parametrize(
    "bad_report",
    [
        "No idle workloads were found in non-system namespaces.",
        "`gke-system-balloon-pod` pods are running in `kube-system`.",
        "On GKE Autopilot, you should delete `gke-system-balloon-pod` to reclaim vCPUs.",
        "On GKE Autopilot, delete `kube-system/gke-system-balloon-pod` to save headroom.",
        "On GKE Autopilot, scale down all `gke-system-balloon-pod` reservations to free spare capacity.",
        "On GKE Autopilot, right-size the `gke-system-balloon-pod` deployment to save headroom.",
    ],
)
def test_report_missing_headroom_or_proposing_removal_fails(bad_report: str) -> None:
    transcript.set(output=bad_report, trajectory=[])
    try:
        verdict = REPORT.verify(timeout_sec=5)
    finally:
        transcript.clear()
    assert verdict.status != "error", verdict.reason
    assert not verdict.success
