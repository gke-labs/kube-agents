"""Reproduce harness CUJ5: read-only cluster namespace and phase status inventory."""

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

SCENARIO_ID = "cuj_harness_05"
READ_ONLY_MILESTONE = "m4-namespace-inventory-remains-read-only"
REQUIRED_INPUT_PHRASES = (
    "active namespaces",
    "default",
    "kube-system",
    "kubeagents-system",
    "phase status",
)

PROMPT = """List the active namespaces on this host cluster and their phase \
status: (1) confirm whether the core Kubernetes namespaces (default and \
kube-system) are present, (2) confirm whether the kubeagents-system harness \
namespace is present, and (3) report the phase status (such as Active) for \
those namespaces. Include the namespace names (default, kube-system, \
kubeagents-system) and their Active phase status in both the task completion \
summary and the full result. Do not create, modify, or delete any cluster \
resources."""

NON_DEFAULT_NS_PHRASE_RE = re.compile(
    r"\bby\s+default\b|\(\s*default\s*\)",
    re.IGNORECASE,
)
CORE_NAMESPACE_RE = re.compile(
    r"(?<![a-z0-9_-])\bdefault\b(?![-_][a-z0-9])"
    r"(?!\s+(?:node\s+)?(?:pool|service\s*account|storage\s*class|backend|route|policy|mode|config\w*|setting\w*|value\w*|profile)\b)"
    r"|\bkube-system\b",
    re.IGNORECASE,
)
HARNESS_NAMESPACE_RE = re.compile(r"\bkubeagents-system\b", re.IGNORECASE)
ACTIVE_PHASE_RE = re.compile(
    r"\bactive\b(?!\s+namespaces?\b)",
    re.IGNORECASE,
)
PROCEDURAL_NS_RESTATEMENT_RE = re.compile(
    r"\A\s*(?:i|we)\s+(?:listed|checked|inspected|queried|verified)\s+"
    r"(?:whether|if|the\s+active\s+namespaces)\b[^:]*\Z",
    re.IGNORECASE,
)
NS_OUTCOME_RE = re.compile(
    r"\b(?:(?:are|is)\s+(?:all\s+|both\s+)?(?:present|active|in\s+active\s+phase)"
    r"|in\s+active\s+(?:phase|status|state)"
    r"|all\s+(?:three\s+|3\s+)?(?:namespaces?\s+)?are\s+active"
    r"|(?:phase|status)\s*[:=]\s*active)\b",
    re.IGNORECASE,
)

ACCEPTANCE_CRITERIA = (
    AcceptanceCriterion(
        "ac01-namespace-request-preserved",
        "The request asks for cluster namespaces (default, kube-system, "
        "kubeagents-system) and their phase status.",
        "projected user input names default, kube-system, kubeagents-system, "
        "and phase status",
    ),
    AcceptanceCriterion(
        "ac02-core-namespaces-reported",
        "The answer confirms the core Kubernetes namespaces default and "
        "kube-system.",
        "delivered answer names default and kube-system in non-error clauses",
    ),
    AcceptanceCriterion(
        "ac03-harness-namespace-reported",
        "The answer confirms the kubeagents-system namespace is present.",
        "delivered answer names kubeagents-system in a non-error clause",
    ),
    AcceptanceCriterion(
        "ac04-active-phase-reported",
        "The answer reports Active phase status for the cluster namespaces.",
        "delivered answer reports Active status in a non-error clause",
    ),
    AcceptanceCriterion(
        "ac05-namespace-inventory-remains-read-only",
        "The namespace inventory check performs no mutations, leaks no "
        "internal LiteLLM proxy aliases, and finishes within the turn "
        "latency budget.",
        "complete normalized tool evidence contains no mutating calls, output "
        "contains no model-default or custom provider leak, and turn latency "
        "is within budget",
    ),
)

MILESTONES = (
    Milestone(
        INTERACTION_COMPLETED_MILESTONE,
        "The namespace inventory conversation finishes successfully.",
        "terminal completed interaction",
    ),
    Milestone(
        KAGE_DELEGATES_MILESTONE,
        "Kage delegates the namespace inventory via kanban_create.",
        "root toolCalls contains a completed kanban_create",
        (INTERACTION_COMPLETED_MILESTONE,),
    ),
    Milestone(
        SPECIALIST_CREATED_MILESTONE,
        "A specialist task is created to list cluster namespaces.",
        "at least one projected specialist task exists",
        (KAGE_DELEGATES_MILESTONE,),
    ),
    Milestone(
        SPECIALIST_COMPLETES_MILESTONE,
        "The delegated specialist namespace task finishes.",
        "at least one specialist task is done after at least one run",
        (SPECIALIST_CREATED_MILESTONE,),
    ),
    Milestone(
        READ_ONLY_MILESTONE,
        "Listing cluster namespaces mutates nothing.",
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
    positive_clauses = [
        clause
        for clause in positive_inventory_clauses(
            answer,
            raw_input,
            "namespaces?",
            "default",
            "kube-system",
            "kubeagents-system",
        )
        if not (
            PROCEDURAL_NS_RESTATEMENT_RE.search(clause)
            and not NS_OUTCOME_RE.search(clause)
        )
    ]
    positive_text = "\n".join(positive_clauses)
    scrubbed_default_text = NON_DEFAULT_NS_PHRASE_RE.sub(" ", positive_text)
    leaks = proxy_leaks(all_visible_text(interaction))
    operations = tool_operations(interaction)
    mutations = sorted(FORBIDDEN_OPERATIONS.intersection(operations))
    refusal = has_clarification_or_refusal(positive_text or answer)

    core_found = sorted(
        {
            m.group(0).casefold()
            for m in CORE_NAMESPACE_RE.finditer(scrubbed_default_text)
        }
    )
    has_core = {"default", "kube-system"}.issubset(core_found) and not refusal
    has_harness_ns = bool(HARNESS_NAMESPACE_RE.search(positive_text)) and not refusal
    has_active = bool(ACTIVE_PHASE_RE.search(positive_text)) and not refusal

    suite = AcceptanceCriteria(ACCEPTANCE_CRITERIA)
    suite.record(
        "ac01-namespace-request-preserved",
        all(phrase in folded_input for phrase in REQUIRED_INPUT_PHRASES),
        raw_input,
    )
    suite.record(
        "ac02-core-namespaces-reported",
        has_core,
        {"coreNamespaces": core_found, "answer": answer},
        blocked_by=output_blocker,
    )
    suite.record(
        "ac03-harness-namespace-reported",
        has_harness_ns,
        {"hasHarnessNamespace": has_harness_ns, "answer": answer},
        blocked_by=output_blocker,
    )
    suite.record(
        "ac04-active-phase-reported",
        has_active,
        {"hasActivePhase": has_active, "answer": answer},
        blocked_by=output_blocker,
    )
    suite.record(
        "ac05-namespace-inventory-remains-read-only",
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


def test_05_namespace_inventory() -> None:
    run_harness_scenario(
        Scenario(
            SCENARIO_ID,
            build_prompt,
            evaluate_acceptance,
            evaluate_backend_milestones,
        )
    )
