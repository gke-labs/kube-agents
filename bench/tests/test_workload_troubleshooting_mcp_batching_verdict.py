"""Unit tests for workload-troubleshooting-mcp-batching-probe verdict regexes.

Pins both objective checks (`skill-names-preferred-gke-mcp-read-tools` and
`skill-prescribes-parallel-mcp-and-batched-cli`) so real reports naming the
four gke MCP read tools and parallel/batched execution pass under any standard
tool-name spelling, while pre-change sequential-CLI reports, missing tools,
write tools, and negated parallel/batching claims fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kube_agents_bench import transcript
from kube_agents_bench.verifiers import ReportContainsVerifier

TASK = (
    Path(__file__).resolve().parent.parent
    / "tasks"
    / "workload-troubleshooting-mcp-batching-probe"
    / "task.yaml"
)
TOOLS_CHECK = "skill-names-preferred-gke-mcp-read-tools"
PATTERN_CHECK = "skill-prescribes-parallel-mcp-and-batched-cli"
TIMEOUT_SEC = 5.0


@pytest.fixture(autouse=True)
def _clean_transcript():
    transcript.clear()
    yield
    transcript.clear()


def _passes(check_name: str, report: str) -> bool:
    doc = yaml.safe_load(TASK.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == check_name]
    transcript.set(report, [], final_message=report)
    return ReportContainsVerifier(**check["check"]).verify(TIMEOUT_SEC).success


def test_tools_check_accepts_all_four_mcp_tools_across_spellings():
    for line in (
        "STEP-1-TO-4-TOOLS: get_k8s_resource, describe_k8s_resource, get_k8s_logs, list_k8s_events, kubectl, gcloud",
        "**STEP-1-TO-4-TOOLS:** `gke:get_k8s_resource`, `gke:describe_k8s_resource`, `gke:get_k8s_logs`, `gke:list_k8s_events`, `kubectl`",
        "STEP-1-TO-4-TOOLS: gke.get_k8s_resource, gke.describe_k8s_resource, gke.get_k8s_logs, gke.list_k8s_events",
        "- STEP-1-TO-4-TOOLS: mcp__gke__get_k8s_resource, mcp__gke__describe_k8s_resource, mcp__gke__get_k8s_logs, mcp__gke__list_k8s_events",
        "STEP-1-TO-4-TOOLS: mcp_gke_get_k8s_resource, mcp_gke_describe_k8s_resource, mcp_gke_get_k8s_logs, mcp_gke_list_k8s_events",
        # Negated write-tool disclaimers must not false-fail.
        "STEP-1-TO-4-TOOLS: get_k8s_resource, describe_k8s_resource, get_k8s_logs, list_k8s_events, kubectl, gcloud (not mutating tools like patch_k8s_resource)",
        "STEP-1-TO-4-TOOLS: get_k8s_resource, describe_k8s_resource, get_k8s_logs, list_k8s_events, kubectl, gcloud (no apply_k8s_manifest, patch_k8s_resource, or delete_k8s_resource)",
        "STEP-1-TO-4-TOOLS: get_k8s_resource, describe_k8s_resource, get_k8s_logs, list_k8s_events; excludes patch_k8s_resource and delete_k8s_resource",
        "STEP-1-TO-4-TOOLS: get_k8s_resource, describe_k8s_resource, get_k8s_logs, list_k8s_events (not mutating tools, e.g. gke.patch_k8s_resource)",
        "STEP-1-TO-4-TOOLS: get_k8s_resource, describe_k8s_resource, get_k8s_logs, list_k8s_events (not mutating tools (patch_k8s_resource, delete_k8s_resource))",
        "STEP-1-TO-4-TOOLS: get_k8s_resource, describe_k8s_resource, get_k8s_logs, list_k8s_events (patch_k8s_resource and delete_k8s_resource are not used)",
        "STEP-1-TO-4-TOOLS: get_k8s_resource (not patch_k8s_resource), describe_k8s_resource, get_k8s_logs, list_k8s_events",
        "STEP-1-TO-4-TOOLS: get_k8s_resource, describe_k8s_resource, get_k8s_logs, list_k8s_events (no gke:patch_k8s_resource or mcp__gke__apply_k8s_manifest)",
    ):
        assert _passes(TOOLS_CHECK, line), line


def test_tools_check_rejects_cli_only_missing_tool_or_write_tool():
    for report in (
        "STEP-1-TO-4-TOOLS: kubectl, gcloud",
        "STEP-1-TO-4-TOOLS: get_k8s_resource, describe_k8s_resource, get_k8s_logs, kubectl",
        "STEP-1-TO-4-TOOLS: get_k8s_resource, describe_k8s_resource, get_k8s_logs, list_k8s_events, patch_k8s_resource",
        "STEP-1-TO-4-TOOLS: get_k8s_resource, describe_k8s_resource, get_k8s_logs, list_k8s_events, gke.patch_k8s_resource",
        "STEP-1-TO-4-TOOLS: get_k8s_resource, describe_k8s_resource, get_k8s_logs, list_k8s_events, apply_k8s_manifest",
        "STEP-1-TO-4-TOOLS: get_k8s_resource, describe_k8s_resource, get_k8s_logs, list_k8s_events, create_node_pool",
        "STEP-1-TO-4-TOOLS: get_k8s_resource, describe_k8s_resource, get_k8s_logs, list_k8s_events, update_node_pool",
        "STEP-1-TO-4-TOOLS: get_k8s_resource, describe_k8s_resource, get_k8s_logs, list_k8s_events, cancel_operation",
        # Negated read-tool listings must fail even when all four names appear on the line.
        "STEP-1-TO-4-TOOLS: kubectl, gcloud (does not mention get_k8s_resource, describe_k8s_resource, get_k8s_logs, or list_k8s_events)",
        "STEP-1-TO-4-TOOLS: kubectl, gcloud (no get_k8s_resource, describe_k8s_resource, get_k8s_logs, or list_k8s_events)",
        "STEP-1-TO-4-TOOLS: kubectl, gcloud; omits get_k8s_resource, describe_k8s_resource, get_k8s_logs, and list_k8s_events",
        "STEP-1-TO-4-TOOLS: kubectl, gcloud -- does not name gke:get_k8s_resource, gke:describe_k8s_resource, gke:get_k8s_logs, or gke:list_k8s_events",
        "STEP-1-TO-4-TOOLS: get_k8s_resource, describe_k8s_resource, get_k8s_logs, kubectl (does not mention list_k8s_events)",
        "STEP-1-TO-4-TOOLS: kubectl, gcloud (get_k8s_resource, describe_k8s_resource, get_k8s_logs, and list_k8s_events are not mentioned)",
        # Positive write tool alongside a separate negated parenthetical must still fail.
        "STEP-1-TO-4-TOOLS: get_k8s_resource, describe_k8s_resource, get_k8s_logs, list_k8s_events, patch_k8s_resource (not delete_k8s_resource)",
        "STEP-1-TO-4-TOOLS: get_k8s_resource, describe_k8s_resource, get_k8s_logs, list_k8s_events (no get-credentials), patch_k8s_resource",
        "STEP-1-TO-4-TOOLS: get_k8s_resource, describe_k8s_resource, get_k8s_logs, list_k8s_events (not delete_k8s_resource); patch_k8s_resource",
        # Tools mentioned only in prose below, not on the STEP-1-TO-4-TOOLS line.
        "STEP-1-TO-4-TOOLS: kubectl, gcloud\n\nget_k8s_resource describe_k8s_resource get_k8s_logs list_k8s_events",
        "",
    ):
        assert not _passes(TOOLS_CHECK, report), report


def test_call_pattern_accepts_parallel_mcp_and_batched_cli():
    for line in (
        "CALL-PATTERN: Emit independent gke MCP reads in parallel in a single turn and batch CLI fallback commands into one compound terminal call.",
        "**CALL-PATTERN:** Run independent MCP tools in parallel and batch read-only kubectl commands into a single shell call.",
        "- CALL-PATTERN: Execute independent reads in one turn via parallel MCP calls or a compound terminal call instead of sequential turns.",
        "CALL-PATTERN: Without running get-credentials, emit gke MCP reads in parallel and batch CLI fallback commands into one compound terminal call.",
        "CALL-PATTERN: Emit gke MCP reads in parallel without running get-credentials and batch CLI fallback commands into one compound terminal call.",
        "CALL-PATTERN: Run gke MCP reads in parallel (no get-credentials needed) and batch CLI fallback reads into one compound terminal call.",
        "CALL-PATTERN: Invoke independent reads as parallel MCP tool calls (not sequential turns) or batch CLI fallback commands into one compound terminal call.",
        "CALL-PATTERN: Run independent reads not one-by-one but in parallel MCP calls or batched into a single compound terminal call.",
        "CALL-PATTERN: Do not run sequentially — emit parallel MCP reads and batch CLI fallback commands into one compound terminal call.",
        "CALL-PATTERN: Do not run sequentially -- emit parallel MCP reads and batch CLI fallback commands into one compound terminal call.",
        "CALL-PATTERN: Do not run sequentially: emit parallel MCP reads and batch CLI fallback commands into one compound terminal call.",
        "CALL-PATTERN: Do not run sequentially - emit parallel MCP reads and batch CLI fallback commands into one compound terminal call.",
        "CALL-PATTERN: Do not: emit parallel MCP reads and batch CLI fallback commands into one compound terminal call.",
        # Negation window must not cross a newline into the following line.
        "CALL-PATTERN: Emit parallel MCP reads and batch CLI fallback commands into one compound terminal call (do not\nrun parallel writes).",
    ):
        assert _passes(PATTERN_CHECK, line), line


def test_call_pattern_rejects_sequential_only_or_negated_claims():
    for report in (
        "CALL-PATTERN: Run each kubectl and gcloud command sequentially one step at a time.",
        "CALL-PATTERN: The skill does not mention parallel calls or batching.",
        "CALL-PATTERN: The skill does not prescribe running commands in parallel or batching them.",
        "CALL-PATTERN: The skill only prescribes sequential kubectl commands and does not instruct parallel or batched execution.",
        "CALL-PATTERN: Sequential single-command kubectl turns; neither parallel tool calls nor batched terminal commands are prescribed.",
        "CALL-PATTERN: The skill prescribes running commands sequentially rather than in parallel or batched into one compound terminal call.",
        "CALL-PATTERN: The skill uses sequential single-command turns instead of parallel tool calls or batched compound terminal commands.",
        "CALL-PATTERN: The skill does not prescribe invoking independent reads in parallel or in a single compound shell call.",
        "CALL-PATTERN: No batching is specified; emit independent reads in parallel.",
        "CALL-PATTERN: Run reads without parallel tool calls or batching.",
        # Negations targeting only the accepted turn/shell synonyms must also fail.
        "CALL-PATTERN: The skill does not invoke independent reads in a single turn or in one shell call.",
        "CALL-PATTERN: Run commands sequentially rather than in the same turn or in a single terminal call.",
        "CALL-PATTERN: Run commands sequentially instead of in one turn or in a single shell call.",
        "CALL-PATTERN: Run commands sequentially without a single turn or one compound terminal call.",
        "CALL-PATTERN: The skill never runs independent reads in the same turn or in one terminal call.",
        "CALL-PATTERN: The skill runs independent reads in a single turn, but does not group CLI commands in one shell call.",
        "CALL-PATTERN: Run commands in parallel only.",
        "CALL-PATTERN: Batch kubectl commands together.",
        "",
    ):
        assert not _passes(PATTERN_CHECK, report), report
