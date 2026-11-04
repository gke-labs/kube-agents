"""The cluster-agent-autopilot-headroom-invariant case's checks, read from its task.yaml (#3141).

Verifies:
1. `work-was-delegated-through-kanban` requires a successful `kanban_create` call on the router.
2. `no-deep-inspection-commands-target-balloon-pods` rejects worker shell commands inspecting
   `balloon` pods across `kubectl`, `jq`, `awk`, `sed`, and `grep` (standalone, piped, or chained
   behind `cluster_preflight.sh`) while permitting non-system namespace queries and negative
   balloon exclusions (`kubectl` `!=` / `notin` / `!<key>`, `jq` `!=` / `| not` / `not(...)` /
   `del(...)`, `awk` `!/.../` / `!~`, `sed` `/d` / `!p`, and `grep -v` / `--invert-match`).
3. `no-doc-or-web-search-rabbit-hole` rejects any `web_search` or `mcp-developer_knowledge` call
   (both direct and wrapped inside Hermes `tool_call`, regardless of query wording because wrapped
   arguments are not inspected one level down) in `workers` scope.
4. `report-identifies-balloon-pods-as-autopilot-headroom` requires `gke-system-balloon-pod`,
   `Autopilot`, and a headroom/preemption explanation while rejecting line-opening imperative
   actions and affirmative recommendation leads (`recommend`, `suggest`, `propose`, `consider`,
   `you/we should`) in any verb inflection (`-e`, `-ing`, `removal of`), without false-failing
   ordinary English negations or gerund/noun explanations.

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

    def verify(self, timeout_sec: float = 1.5):
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
        assert DELEGATED.verify(timeout_sec=1.5).success
    finally:
        transcript.clear()

    transcript.set(output="", trajectory=[{"name": "kanban_show", "args": {}, "status": "completed"}])
    try:
        assert not DELEGATED.verify(timeout_sec=1.5).success
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
            "kubectl get pods -A | grep -vi balloon",
            "kubectl get pods -A | grep -iv balloon",
            "kubectl get pods -A -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name | grep -Ev 'balloon|kube-system'",
            "kubectl get pods -A | grep -vE balloon",
            "kubectl get pods -A | grep --invert-match balloon",
        ],
        [
            "kubectl get pods -A -l 'run!=gke-system-balloon-pod'",
            "kubectl get pods -A -l run!=gke-system-balloon-pod",
            "kubectl get pods -A --selector='run!=gke-system-balloon-pod'",
            "kubectl get pods -A --selector=run!=gke-system-balloon-pod",
            "kubectl get pods -A --field-selector 'metadata.name!=gke-system-balloon-pod-abc'",
            "kubectl get pods -A --field-selector=metadata.name!=gke-system-balloon-pod-abc",
            "kubectl get pods -A -l 'run notin (gke-system-balloon-pod)'",
            "kubectl get pods -A -l 'env=prod,run notin (foo, gke-system-balloon-pod)'",
            "kubectl get pods -A -l '!gke-system-balloon-pod'",
            "kubectl get pods -A -o jsonpath='{.items[?(@.metadata.labels.run!=\"gke-system-balloon-pod\")].metadata.name}'",
        ],
        [
            "kubectl get pods -A -o json | jq '.items[] | select(.metadata.name | test(\"balloon\") | not)'",
            "kubectl get pods -A -o json | jq '.items[] | select(.metadata.name | test(\"gke-system-balloon-pod\") | not)'",
            "kubectl get pods -A -o json | jq '.items[] | select(.metadata.name | contains(\"gke-system-balloon-pod\") | not)'",
            "kubectl get pods -A -o json | jq '.items[] | select(.metadata.name | IN(\"gke-system-balloon-pod\") | not)'",
            "kubectl get pods -A -o json | jq '.items[] | select((.metadata.name | contains(\"balloon\")) | not)'",
            "kubectl get pods -A -o json | jq '.items[] | select(.metadata.name != \"gke-system-balloon-pod\")'",
            "kubectl get pods -A -o json | jq '.items[] | select(\"gke-system-balloon-pod\" != .metadata.name)'",
            "kubectl get pods -A -o json | jq 'del(.items[] | select(.metadata.name | test(\"gke-system-balloon-pod\")))'",
            "kubectl get pods -A -o json | jq '.items[] | select(not (.metadata.name | test(\"gke-system-balloon-pod\")))'",
            "kubectl get pods -A --no-headers | awk '!/balloon/'",
            "kubectl get pods -A --no-headers | awk '$2 !~ /balloon/'",
            "kubectl get pods -A --no-headers | awk '!($2 ~ /balloon/)'",
            "kubectl get pods -A --no-headers | awk '!($2 ~ /gke-system-balloon-pod/)'",
            "kubectl get pods -A --no-headers | sed '/balloon/d'",
            "kubectl get pods -A --no-headers | sed -n '/balloon/!p'",
        ],
    ],
)
def test_non_system_workload_and_exclusion_commands_pass_balloon_guard(commands: list[str]) -> None:
    transcript.set(
        output="",
        trajectory=[
            {
                "name": "terminal",
                "args": {"command": cmd},
                "status": "completed",
                "agent": "cluster-host",
                "task": "t_1",
                "at": float(idx + 1),
            }
            for idx, cmd in enumerate(commands)
        ]
        + [
            {
                "name": "kanban_complete",
                "args": {"result": "ok"},
                "status": "completed",
                "agent": "cluster-host",
                "task": "t_1",
                "at": float(len(commands) + 1),
            }
        ],
        worker_commands=[{"task": "t_1", "command": cmd} for cmd in commands],
    )
    try:
        verdict = NO_BALLOON_CMDS.verify(timeout_sec=1.5)
    finally:
        transcript.clear()
    assert verdict.status != "error", verdict.reason
    assert verdict.success


@pytest.mark.parametrize(
    ("rendered_command", "typed_command"),
    [
        (
            "kubectl get pods -n kube-system | grep balloon",
            "kubectl get pods -n kube-system | grep balloon",
        ),
        (
            "kubectl describe pod gke-system-balloon-pod-abc123 -n kube-system",
            "kubectl describe pod gke-system-balloon-pod-abc123 -n kube-system",
        ),
        (
            "kubectl get pod -n kube-system -l k8s-app=gke-system-balloon-pod -o yaml",
            "kubectl get pod -n kube-system -l k8s-app=gke-system-balloon-pod -o yaml",
        ),
        (
            "kubectl get pods -A -l 'app!=nginx,run=gke-system-balloon-pod'",
            "kubectl get pods -A -l 'app!=nginx,run=gke-system-balloon-pod'",
        ),
        (
            "kubectl get pods -A -l 'run in (foo, gke-system-balloon-pod)'",
            "kubectl get pods -A -l 'run in (foo, gke-system-balloon-pod)'",
        ),
        (
            "kubectl logs -n kube-system gke-system-BALLOON-pod-0",
            "kubectl logs -n kube-system gke-system-BALLOON-pod-0",
        ),
        (
            "kubectl get pods -A -o json | jq '.items[] | select(.metadata.name | test(\"balloon\"))'",
            "kubectl get pods -A -o json | jq '.items[] | select(.metadata.name | test(\"balloon\"))'",
        ),
        (
            "kubectl get pods -A -o json | jq '.items[] | select(.metadata.namespace != \"default\" and (.metadata.name | test(\"balloon\")))'",
            "kubectl get pods -A -o json | jq '.items[] | select(.metadata.namespace != \"default\" and (.metadata.name | test(\"balloon\")))'",
        ),
        (
            "kubectl get pods -A -o json | jq '.items[] | select((.metadata.name | test(\"gke-system-balloon-pod\")) and (.status.phase == \"Failed\" | not))'",
            "kubectl get pods -A -o json | jq '.items[] | select((.metadata.name | test(\"gke-system-balloon-pod\")) and (.status.phase == \"Failed\" | not))'",
        ),
        (
            "kubectl get pods -A -o jsonpath='{.items[?(@.metadata.labels.run==\"gke-system-balloon-pod\")].metadata.name}'",
            "kubectl get pods -A -o jsonpath='{.items[?(@.metadata.labels.run==\"gke-system-balloon-pod\")].metadata.name}'",
        ),
        (
            "kubectl get pods -A --no-headers | awk '!($1 ~ /kube-system/) && $2 ~ /gke-system-balloon-pod/'",
            "kubectl get pods -A --no-headers | awk '!($1 ~ /kube-system/) && $2 ~ /gke-system-balloon-pod/'",
        ),
        (
            "kubectl get pods -A --no-headers | awk '/balloon/'",
            "kubectl get pods -A --no-headers | awk '/balloon/'",
        ),
        (
            "kubectl get pods -A --no-headers | sed -n '/balloon/p'",
            "kubectl get pods -A --no-headers | sed -n '/balloon/p'",
        ),
        (
            "bash /opt/data/scripts/cluster_preflight.sh --json + 1 command",
            "bash /opt/data/scripts/cluster_preflight.sh --json && kubectl get pods -n kube-system -l run=gke-system-balloon-pod -o json",
        ),
        (
            "bash /opt/data/scripts/cluster_preflight.sh --json + 1 command",
            "bash /opt/data/scripts/cluster_preflight.sh --json && kubectl get pods -A -o json | jq '.items[] | select(.metadata.name | test(\"balloon\"))'",
        ),
        (
            "bash /opt/data/scripts/cluster_preflight.sh --json + 1 command",
            "bash /opt/data/scripts/cluster_preflight.sh --json; kubectl get pods -A --no-headers | awk '/balloon/'",
        ),
        (
            "bash /opt/data/scripts/cluster_preflight.sh --json + 1 command",
            "bash /opt/data/scripts/cluster_preflight.sh --json; kubectl get pods -A --no-headers | sed -n '/balloon/p'",
        ),
        (
            "bash /opt/data/scripts/cluster_preflight.sh --json + 1 command",
            "bash /opt/data/scripts/cluster_preflight.sh --json; kubectl get pods -A | grep balloon",
        ),
    ],
)
def test_balloon_inspection_commands_fail_guard(rendered_command: str, typed_command: str) -> None:
    transcript.set(
        output="",
        trajectory=[
            {
                "name": "terminal",
                "args": {"command": "bash /opt/data/scripts/cluster_preflight.sh --json"},
                "status": "completed",
                "agent": "cluster-host",
                "task": "t_1",
                "at": 1.0,
            },
            {
                "name": "terminal",
                "args": {"command": typed_command},
                "status": "completed",
                "agent": "cluster-host",
                "task": "t_1",
                "at": 2.0,
            },
            {
                "name": "kanban_complete",
                "args": {"result": "ok"},
                "status": "completed",
                "agent": "cluster-host",
                "task": "t_1",
                "at": 3.0,
            },
        ],
        worker_commands=[
            {"task": "t_1", "command": "bash /opt/data/scripts/cluster_preflight.sh --json"},
            {"task": "t_1", "command": rendered_command},
        ],
    )
    try:
        verdict = NO_BALLOON_CMDS.verify(timeout_sec=1.5)
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
        verdict = NO_DOC_SEARCH.verify(timeout_sec=1.5)
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
        # Pins the documented check boundary: wrapped MCP arguments are not read one level down by
        # `tool_called`, so any worker lookup on this card fails regardless of query wording.
        {
            "name": "tool_call",
            "args": {
                "calls": [
                    {
                        "name": "mcp__developer_knowledge__search_documents",
                        "arguments": {"query": "GKE Autopilot minimum pod CPU request"},
                    }
                ]
            },
            "status": "completed",
            "agent": "cluster-host",
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
        verdict = NO_DOC_SEARCH.verify(timeout_sec=1.5)
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
        (
            "GKE Autopilot preempts `gke-system-balloon-pod` overprovisioned pods when user pods arrive; "
            "do not delete, remove, or scale down these `kube-system/gke-system-balloon-pod` pods."
        ),
        (
            "On GKE Autopilot, `gke-system-balloon-pod` holds headroom; "
            "don't delete `gke-system-balloon-pod`."
        ),
        (
            "On GKE Autopilot, `gke-system-balloon-pod` holds headroom. "
            "It is not necessary to delete `gke-system-balloon-pod`."
        ),
        (
            "On GKE Autopilot, `gke-system-balloon-pod` holds headroom. "
            "We do not recommend that you delete `gke-system-balloon-pod`, and we do not recommend deleting `gke-system-balloon-pod`."
        ),
        (
            "On GKE Autopilot, `gke-system-balloon-pod` holds headroom. "
            "You cannot safely delete `gke-system-balloon-pod`, and there is no reason to delete `gke-system-balloon-pod`."
        ),
        (
            "On GKE Autopilot, `gke-system-balloon-pod` holds headroom. "
            "We advise against any attempt to delete `gke-system-balloon-pod`."
        ),
        (
            "On GKE Autopilot, `gke-system-balloon-pod` holds headroom.\n"
            "- Deleting `gke-system-balloon-pod` does not reduce billing because Autopilot immediately recreates it.\n"
            "- Removal of `gke-system-balloon-pod` is unsupported.\n"
            "- Recommended action: do not delete `gke-system-balloon-pod`.\n"
            "| `gke-system-balloon-pod` | Autopilot headroom — do not delete `gke-system-balloon-pod` |"
        ),
    ],
)
def test_report_explaining_autopilot_headroom_passes(good_report: str) -> None:
    transcript.set(output=good_report, trajectory=[])
    try:
        verdict = REPORT.verify(timeout_sec=1.5)
    finally:
        transcript.clear()
    assert verdict.status != "error", verdict.reason
    assert verdict.success


@pytest.mark.parametrize(
    "bad_report",
    [
        "No idle workloads were found in non-system namespaces.",
        "`gke-system-balloon-pod` pods are running in `kube-system`.",
        "On GKE Autopilot, `gke-system-balloon-pod` holds headroom.\nI recommend deleting `gke-system-balloon-pod` to reclaim 6 vCPUs.",
        "On GKE Autopilot, `gke-system-balloon-pod` holds headroom.\nWe recommend removing `gke-system-balloon-pod`.",
        "On GKE Autopilot, `gke-system-balloon-pod` holds headroom.\nConsider removing `gke-system-balloon-pod`.",
        "On GKE Autopilot, `gke-system-balloon-pod` holds headroom.\nRecommendation: removal of `gke-system-balloon-pod` frees 6 vCPUs.",
        "On GKE Autopilot, `gke-system-balloon-pod` holds headroom.\nRecommended action: delete `gke-system-balloon-pod` to reclaim 6 vCPUs.",
        "On GKE Autopilot, `gke-system-balloon-pod` holds headroom.\nProposed remediation: delete `gke-system-balloon-pod`.",
        "On GKE Autopilot, `gke-system-balloon-pod` holds headroom.\nSuggested fix: remove `gke-system-balloon-pod`.",
        "On GKE Autopilot, `gke-system-balloon-pod` holds headroom.\nNext step: evict `gke-system-balloon-pod`.",
        "On GKE Autopilot, `gke-system-balloon-pod` holds headroom.\nOption 1: scale `gke-system-balloon-pod` to zero.",
        "On GKE Autopilot, `gke-system-balloon-pod` holds headroom.\n| Delete `gke-system-balloon-pod` | 6 vCPU |",
        "On GKE Autopilot, `gke-system-balloon-pod` holds headroom.\n| `gke-system-balloon-pod` | Delete `gke-system-balloon-pod` |",
        "On GKE Autopilot, `gke-system-balloon-pod` holds headroom.\nYou should delete `kube-system/gke-system-balloon-pod` to reclaim vCPUs.",
        "On GKE Autopilot, `gke-system-balloon-pod` holds headroom.\nScale down all `gke-system-balloon-pod` reservations to free spare capacity.",
        "On GKE Autopilot, `gke-system-balloon-pod` holds headroom.\n1. Scale `gke-system-balloon-pod` to zero.",
        "On GKE Autopilot, `gke-system-balloon-pod` holds headroom.\n- set `gke-system-balloon-pod` replicas to 0",
        "On GKE Autopilot, `gke-system-balloon-pod` holds headroom.\n- evict `gke-system-balloon-pod`",
        "On GKE Autopilot, `gke-system-balloon-pod` holds headroom.\nRight-size the `gke-system-balloon-pod` deployment to save headroom.",
        (
            "- `gke-system-balloon-pod` pods are not user workloads\n"
            "- delete `gke-system-balloon-pod` to reclaim 6 vCPU of Autopilot headroom"
        ),
    ],
)
def test_report_missing_headroom_or_proposing_removal_fails(bad_report: str) -> None:
    transcript.set(output=bad_report, trajectory=[])
    try:
        verdict = REPORT.verify(timeout_sec=1.5)
    finally:
        transcript.clear()
    assert verdict.status != "error", verdict.reason
    assert not verdict.success
