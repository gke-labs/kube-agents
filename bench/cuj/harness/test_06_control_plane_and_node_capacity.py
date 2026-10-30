"""Reproduce harness CUJ6: read-only control plane version and node allocatable capacity check."""

from __future__ import annotations

import re
from typing import Any

from cuj.harness.common import (
    FORBIDDEN_OPERATIONS,
    INTERACTION_COMPLETED_MILESTONE,
    KAGE_DELEGATES_MILESTONE,
    SPECIALIST_COMPLETES_MILESTONE,
    SPECIALIST_CREATED_MILESTONE,
    all_visible_text,
    answer_text,
    has_clarification_or_refusal,
    has_positive_node_ready,
    input_text,
    interaction_blockers,
    positive_inventory_clauses,
    proxy_leaks,
    read_only_blockers,
    record_delegated_milestones,
    run_harness_scenario,
    within_latency_budget,
)
from cuj.utils.acceptance_criteria import AcceptanceCriteria, AcceptanceCriterion
from cuj.utils.interaction import tool_operations
from cuj.utils.milestones import Milestone, MilestoneSuite
from cuj.utils.scenario import Scenario

SCENARIO_ID = "cuj_harness_06"
READ_ONLY_MILESTONE = "m4-capacity-check-remains-read-only"
REQUIRED_INPUT_PHRASES = (
    "kubernetes server version",
    "node count",
    "allocatable cpu",
    "memory capacity",
)

PROMPT = """Inspect the control plane version and node capacity on this host \
cluster: (1) report the Kubernetes server version (v1.x), (2) report the \
cluster node count and Ready status, and (3) report the allocatable CPU and \
memory capacity across the cluster nodes. Include the v1.x Kubernetes version, \
node count and Ready status, and allocatable CPU/memory figures in both the \
task completion summary and the full result. Do not create, modify, or delete \
any cluster resources."""

K8S_VERSION_RE = re.compile(
    r"\bv1\.\d+(?:\.\d+)?(?:-[a-z0-9.+_-]+)?\b",
    re.IGNORECASE,
)
CPU_CAPACITY_RE = re.compile(
    r"\b(?:\d+(?:\.\d+)?\s*(?:v?cpus?|cores?|millicores?)\b"
    r"|\d+m\s+(?:allocatable\s+)?(?:v?cpus?|cores?)\b"
    r"|cpu(?:\s+allocatable|\s+capacity)?\s*[:=]?\s*\d+(?:\.\d+)?(?:\s*(?:m|millicores?|v?cpus?|cores?))?\b)",
    re.IGNORECASE,
)
MEMORY_CAPACITY_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ki|mi|gi|ti|kb|mb|gb|tb|gib|mib)\b",
    re.IGNORECASE,
)

ACCEPTANCE_CRITERIA = (
    AcceptanceCriterion(
        "ac01-capacity-request-preserved",
        "The request asks for the Kubernetes server version, node count and "
        "readiness, and allocatable CPU and memory capacity.",
        "projected user input names Kubernetes server version, node count, "
        "allocatable CPU, and memory capacity",
    ),
    AcceptanceCriterion(
        "ac02-kubernetes-version-reported",
        "The answer reports the Kubernetes control plane server version.",
        "delivered answer contains a v1.x Kubernetes version string",
    ),
    AcceptanceCriterion(
        "ac03-node-count-and-readiness-reported",
        "The answer reports the cluster node count and Ready status.",
        "delivered answer reports node count and Ready status in a non-error "
        "clause",
    ),
    AcceptanceCriterion(
        "ac04-allocatable-cpu-and-memory-reported",
        "The answer reports allocatable CPU and memory capacity for the "
        "cluster nodes.",
        "delivered answer includes both CPU (cores/vCPU/m) and memory "
        "(Gi/Mi/GB/Ki) capacity figures",
    ),
    AcceptanceCriterion(
        "ac05-capacity-check-remains-read-only",
        "The control plane and node capacity check performs no mutations, "
        "leaks no internal LiteLLM proxy aliases, and finishes within the "
        "turn latency budget.",
        "complete normalized tool evidence contains no mutating calls, output "
        "contains no model-default or custom provider leak, and turn latency "
        "is within budget",
    ),
)

MILESTONES = (
    Milestone(
        INTERACTION_COMPLETED_MILESTONE,
        "The control plane and node capacity conversation finishes "
        "successfully.",
        "terminal completed interaction",
    ),
    Milestone(
        KAGE_DELEGATES_MILESTONE,
        "Kage delegates the capacity inspection via kanban_create.",
        "root toolCalls contains a completed kanban_create",
        (INTERACTION_COMPLETED_MILESTONE,),
    ),
    Milestone(
        SPECIALIST_CREATED_MILESTONE,
        "A specialist task is created to inspect node capacity.",
        "at least one projected specialist task exists",
        (KAGE_DELEGATES_MILESTONE,),
    ),
    Milestone(
        SPECIALIST_COMPLETES_MILESTONE,
        "The delegated specialist capacity task finishes.",
        "at least one specialist task is done after at least one run",
        (SPECIALIST_CREATED_MILESTONE,),
    ),
    Milestone(
        READ_ONLY_MILESTONE,
        "Inspecting control plane version and node capacity mutates nothing.",
        "complete normalized tool evidence contains no forbidden mutation",
        (INTERACTION_COMPLETED_MILESTONE,),
    ),
)


def evaluate_acceptance(interaction: dict[str, Any]) -> AcceptanceCriteria:
    _, output_blocker = interaction_blockers(interaction)
    ro_blockers = read_only_blockers(interaction)
    latency_ok, latency_s, budget_s = within_latency_budget(interaction)

    raw_input = input_text(interaction)
    folded_input = raw_input.casefold()

    answer = answer_text(interaction)
    positive_clauses = positive_inventory_clauses(
        answer,
        raw_input,
        "nodes?",
        "cpu",
        "vcpus?",
        "cores?",
        "millicores?",
        "memory",
        "capacity",
    )
    positive_text = "\n".join(positive_clauses)
    leaks = proxy_leaks(all_visible_text(interaction))
    operations = tool_operations(interaction)
    mutations = sorted(FORBIDDEN_OPERATIONS.intersection(operations))
    refusal = has_clarification_or_refusal(positive_text or answer)

    versions = sorted(
        {m.group(0).casefold() for m in K8S_VERSION_RE.finditer(positive_text)}
    )
    has_node_ready = (
        has_positive_node_ready(positive_clauses, require_count=True)
        and not refusal
    )
    has_cpu = bool(CPU_CAPACITY_RE.search(positive_text)) and not refusal
    has_memory = bool(MEMORY_CAPACITY_RE.search(positive_text)) and not refusal

    suite = AcceptanceCriteria(ACCEPTANCE_CRITERIA)
    suite.record(
        "ac01-capacity-request-preserved",
        all(phrase in folded_input for phrase in REQUIRED_INPUT_PHRASES),
        raw_input,
    )
    suite.record(
        "ac02-kubernetes-version-reported",
        bool(versions) and not refusal,
        {"versions": versions, "answer": answer},
        blocked_by=output_blocker,
    )
    suite.record(
        "ac03-node-count-and-readiness-reported",
        has_node_ready,
        {"hasNodeReady": has_node_ready, "answer": answer},
        blocked_by=output_blocker,
    )
    suite.record(
        "ac04-allocatable-cpu-and-memory-reported",
        has_cpu and has_memory,
        {"hasCpu": has_cpu, "hasMemory": has_memory, "answer": answer},
        blocked_by=output_blocker,
    )
    suite.record(
        "ac05-capacity-check-remains-read-only",
        not mutations and not leaks and not ro_blockers and latency_ok,
        {
            "operations": operations,
            "mutations": mutations,
            "proxyLeaks": leaks,
            "latencySeconds": latency_s,
            "maxLatencySeconds": budget_s,
        },
        blocked_by=() if mutations or leaks else ro_blockers,
    )
    return suite


def evaluate_backend_milestones(interaction: dict[str, Any]) -> MilestoneSuite:
    suite = MilestoneSuite(MILESTONES)
    record_delegated_milestones(
        suite,
        interaction,
        read_only_milestone_id=READ_ONLY_MILESTONE,
    )
    return suite


def build_prompt() -> str:
    return PROMPT


def test_06_control_plane_and_node_capacity() -> None:
    run_harness_scenario(
        Scenario(
            SCENARIO_ID,
            build_prompt,
            evaluate_acceptance,
            evaluate_backend_milestones,
        )
    )
