"""Reproduce harness CUJ8: read-only PersistentVolumeClaim and StorageClass health check."""

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
    non_echo_clauses,
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

SCENARIO_ID = "cuj_harness_08"
READ_ONLY_MILESTONE = "m4-storage-check-remains-read-only"
REQUIRED_INPUT_PHRASES = (
    "persistentvolumeclaim",
    "kubeagents-system",
    "binding status",
    "storageclass",
)

PROMPT = """Inspect the storage and PersistentVolumeClaim health in the \
kubeagents-system namespace on this host cluster: (1) list the \
PersistentVolumeClaim resources in kubeagents-system, (2) report their \
binding status and requested capacity, and (3) report the StorageClass \
backing those claims in kubeagents-system. Include the \
PersistentVolumeClaim names, binding status, requested capacity, and \
StorageClass in both the task completion summary and the full result. Do \
not create, modify, or delete any cluster resources."""

PVC_NAME_RE = re.compile(
    r"\b(?:"
    r"platform-agent-(?!host\b)(?:"
    r"(?:gateway|shell|credential-proxy|sandbox)-(?:pvc|data|storage|workspaces?)[a-z0-9-]*"
    r"|(?!gateway\b|shell\b|credential-proxy\b|sandbox\b)[a-z0-9-]+"
    r")"
    r"|[a-z0-9]+(?:-[a-z0-9]+)+-pvc"
    r"|pvc-[a-z0-9-]+"
    r")\b",
    re.IGNORECASE,
)
BOUND_STATUS_RE = re.compile(
    r"(?<!not\s)\bbound\b",
    re.IGNORECASE,
)
NEGATED_UNBOUND_PHRASE_RE = re.compile(
    r"\b(?:no|not|none|nothing|neither|never|without|(?<!-)0|zero"
    r"|isn't|aren't|wasn't|weren't)\b[^,:\n;]{0,35}"
    r"\b(?:pending|lost|unbound|released|failed)\b"
    r"(?:\s*(?:,|or|and|nor)\s*(?:pending|lost|unbound|released|failed)\b)*",
    re.IGNORECASE,
)
UNBOUND_STATUS_RE = re.compile(
    r"\b(?:pending|lost|unbound|released|failed)\b"
    r"|\b(?:not|never|isn't|aren't|wasn't|weren't)\s+(?:yet\s+|currently\s+)?bound\b"
    r"|\b(?:(?<!-)0|zero|no|none|neither)\s+"
    r"(?:(?:of\s+the\s+)?(?:[a-z0-9_-]+\s+){0,2}?"
    r"(?:pvcs?|persistentvolumeclaims?|claims?|volumes?)\s+"
    r"(?:are\s+|is\s+|were\s+|was\s+)?)?bound\b"
    r"|\bbound\s*[:=]\s*(?:false|no|0|unknown|none)\b",
    re.IGNORECASE,
)
STORAGE_CLASS_OR_CAPACITY_RE = re.compile(
    r"\b(?:"
    r"standard-rwx|standard-rwo|premium-rwo|balanced-rwo"
    r"|(?:storage\s*class|class)\s*(?:is\s+|[:=]\s*)?[`'\"]?standard[`'\"]?"
    r"|standard\s+storage\s*class"
    r"|\d+(?:\.\d+)?\s*(?:gi|mi|ti|ki|gib|mib|tib|kib|gb|mb|tb|kb)"
    r")\b",
    re.IGNORECASE,
)

ACCEPTANCE_CRITERIA = (
    AcceptanceCriterion(
        "ac01-storage-request-preserved",
        "The request asks for PersistentVolumeClaims, binding status, and "
        "StorageClass in kubeagents-system.",
        "projected user input names PersistentVolumeClaim, kubeagents-system, "
        "binding status, and StorageClass",
    ),
    AcceptanceCriterion(
        "ac02-kubeagents-pvc-reported",
        "The answer identifies the PersistentVolumeClaims in kubeagents-system.",
        "delivered answer names the kubeagents-system PVC resources",
    ),
    AcceptanceCriterion(
        "ac03-pvc-bound-status-reported",
        "The answer confirms the PVCs in kubeagents-system are Bound.",
        "delivered answer reports Bound status without Pending/Lost errors",
    ),
    AcceptanceCriterion(
        "ac04-storage-class-or-capacity-reported",
        "The answer reports the StorageClass or provisioned capacity for the "
        "kubeagents-system PVCs.",
        "delivered answer names the StorageClass (such as standard-rwx or "
        "standard-rwo) or capacity in Gi/Mi",
    ),
    AcceptanceCriterion(
        "ac05-storage-check-remains-read-only",
        "The storage and PVC health check performs no mutations, leaks no "
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
        "The storage and PVC health conversation finishes successfully.",
        "terminal completed interaction",
    ),
    Milestone(
        KAGE_DELEGATES_MILESTONE,
        "Kage delegates the storage and PVC inspection via kanban_create.",
        "root toolCalls contains a completed kanban_create",
        (INTERACTION_COMPLETED_MILESTONE,),
    ),
    Milestone(
        SPECIALIST_CREATED_MILESTONE,
        "A specialist task is created to inspect kubeagents-system PVCs.",
        "at least one projected specialist task exists",
        (KAGE_DELEGATES_MILESTONE,),
    ),
    Milestone(
        SPECIALIST_COMPLETES_MILESTONE,
        "The delegated specialist PVC inspection finishes.",
        "at least one specialist task is done after at least one run",
        (SPECIALIST_CREATED_MILESTONE,),
    ),
    Milestone(
        READ_ONLY_MILESTONE,
        "Inspecting PVCs and StorageClasses mutates nothing.",
        "complete normalized tool evidence contains no forbidden mutation",
        (INTERACTION_COMPLETED_MILESTONE,),
    ),
)


def _has_unbound_status(clause: str) -> bool:
    scrubbed = NEGATED_UNBOUND_PHRASE_RE.sub(" ", clause)
    return bool(UNBOUND_STATUS_RE.search(scrubbed))


def evaluate_acceptance(interaction: dict[str, Any]) -> AcceptanceCriteria:
    _, output_blocker = interaction_blockers(interaction)
    ro_blockers = read_only_blockers(interaction)
    latency_ok, latency_s, budget_s = within_latency_budget(interaction)

    raw_input = input_text(interaction)
    folded_input = raw_input.casefold()

    answer = answer_text(interaction)
    all_clauses = non_echo_clauses(answer, raw_input)
    positive_clauses = positive_inventory_clauses(
        answer,
        raw_input,
        "pvcs?",
        "persistentvolumeclaims?",
        "claims?",
    )
    positive_text = "\n".join(positive_clauses)
    leaks = proxy_leaks(all_visible_text(interaction))
    operations = tool_operations(interaction)
    mutations = sorted(FORBIDDEN_OPERATIONS.intersection(operations))
    refusal = has_clarification_or_refusal(positive_text or answer)

    has_pvc = bool(PVC_NAME_RE.search(positive_text)) and not refusal
    has_bound = (
        any(
            BOUND_STATUS_RE.search(clause) and not _has_unbound_status(clause)
            for clause in positive_clauses
        )
        and not any(_has_unbound_status(clause) for clause in all_clauses)
        and not refusal
    )
    storage_details = sorted(
        {
            m.group(0).casefold()
            for m in STORAGE_CLASS_OR_CAPACITY_RE.finditer(positive_text)
        }
    )

    suite = AcceptanceCriteria(ACCEPTANCE_CRITERIA)
    suite.record(
        "ac01-storage-request-preserved",
        all(phrase in folded_input for phrase in REQUIRED_INPUT_PHRASES),
        raw_input,
    )
    suite.record(
        "ac02-kubeagents-pvc-reported",
        has_pvc,
        {"hasPvc": has_pvc, "answer": answer},
        blocked_by=output_blocker,
    )
    suite.record(
        "ac03-pvc-bound-status-reported",
        has_bound,
        {"hasBoundStatus": has_bound, "answer": answer},
        blocked_by=output_blocker,
    )
    suite.record(
        "ac04-storage-class-or-capacity-reported",
        bool(storage_details) and not refusal,
        {"storageDetails": storage_details, "answer": answer},
        blocked_by=output_blocker,
    )
    suite.record(
        "ac05-storage-check-remains-read-only",
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


def test_08_storage_and_pvc_health() -> None:
    run_harness_scenario(
        Scenario(
            SCENARIO_ID,
            build_prompt,
            evaluate_acceptance,
            evaluate_backend_milestones,
        )
    )
