"""Reproduce harness CUJ9: read-only NetworkPolicy isolation posture in kubeagents-system."""

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

SCENARIO_ID = "cuj_harness_09"
READ_ONLY_MILESTONE = "m4-netpol-check-remains-read-only"
REQUIRED_INPUT_PHRASES = (
    "networkpolicy",
    "kubeagents-system",
    "policy types",
    "pod selectors",
)

PROMPT = """Inspect the Kubernetes NetworkPolicy resources in the \
kubeagents-system namespace on this host cluster: (1) list the NetworkPolicy \
names in kubeagents-system, (2) report their policy types, and \
(3) summarize the pod selectors and network isolation posture they enforce. \
Include the NetworkPolicy names, policy types, and pod selector summary in \
both the task completion summary and the full result. Do not create, modify, \
or delete any cluster resources."""

_NON_NETPOL_FOLLOW_NOUN = (
    r"(?!\s+(?:deployments?|statefulsets?|replicasets?|pods?|services?"
    r"|svc|containers?|workloads?)\b)"
)
NETPOL_NAME_RE = re.compile(
    rf"\b(?:"
    rf"platform-agent-(?:shell|credential-proxy|sandbox)"
    rf"(?:-(?:netpol|network-policy|policy|isolation|egress|ingress))?(?!-[a-z0-9])"
    rf"|platform-agent-gateway-(?:netpol|network-policy|policy|isolation|egress|ingress)(?!-[a-z0-9])"
    rf"|platform-agent-(?:gw|cred|a2a)-mon-netpol(?!-[a-z0-9])"
    rf"|litellm-(?:netpol|network-policy|policy|isolation|egress|ingress)(?!-[a-z0-9])"
    rf"|kube-agents-web-console(?:-(?:netpol|network-policy|policy))?(?!-[a-z0-9])"
    rf"|kube-agents-[a-z0-9-]+-(?:netpol|network-policy|policy)"
    rf")\b{_NON_NETPOL_FOLLOW_NOUN}",
    re.IGNORECASE,
)
UNRESTRICTED_NETPOL_CLAUSE_RE = re.compile(
    r"\b(?:not\s+isolated|no\s+(?:network\s+)?isolation"
    r"|unrestricted\s+ingress\s+and\s+egress"
    r"|(?:accepts?|allows?|permits?)\s+all\s+(?:ingress\s+and\s+egress|traffic)"
    r"|(?:pods?|workloads?|deployments?|statefulsets?|namespace|traffic)\s+"
    r"(?:in\s+[a-z0-9-]+\s+)?(?:have|has|are|is)\s+unrestricted)\b",
    re.IGNORECASE,
)
RESTRICTION_PREDICATE_RE = re.compile(
    r"\b(?:only|restrict\w*|deny|denies|denied|blocks?|blocked"
    r"|isolat\w*|selects?|podselector|matchlabels|enforc\w*)\b",
    re.IGNORECASE,
)
POLICY_DIRECTION_RE = re.compile(
    r"\b(?:ingress|egress)\b",
    re.IGNORECASE,
)
PROMPT_PHRASE_SCRUB_RE = re.compile(
    r"(?<!\bsandbox\s)(?<!\btargeted\s)\bpod\s+selectors?\b"
    r"|\b(?:network\s+)?isolation\s+posture\b",
    re.IGNORECASE,
)
ISOLATION_SUMMARY_RE = re.compile(
    r"\b(?:podselector|matchlabels|traffic|allow\w*|restrict\w*"
    r"|deny|denies|denied|blocks?|blocked|enforc\w*|selects?|targets?"
    r"|sandbox\s+pod\s+selectors?|egress\s+to|ingress\s+from"
    r"|isolat\w*\s+(?:traffic|pods?|egress|ingress|workloads?|sandboxes?))\b"
    r"|\bapp(?:\.kubernetes\.io/name)?\s*=",
    re.IGNORECASE,
)

ACCEPTANCE_CRITERIA = (
    AcceptanceCriterion(
        "ac01-netpol-request-preserved",
        "The request asks for NetworkPolicy names, policy types, and pod "
        "selectors in kubeagents-system.",
        "projected user input names NetworkPolicy, kubeagents-system, policy "
        "types, and pod selectors",
    ),
    AcceptanceCriterion(
        "ac02-kubeagents-network-policies-named",
        "The answer names the NetworkPolicy resources in kubeagents-system.",
        "delivered answer names at least one kubeagents-system NetworkPolicy",
    ),
    AcceptanceCriterion(
        "ac03-policy-direction-reported",
        "The answer reports the NetworkPolicy direction (Ingress or Egress).",
        "delivered answer reports Ingress and/or Egress policy types",
    ),
    AcceptanceCriterion(
        "ac04-isolation-posture-summarized",
        "The answer summarizes the pod selectors or network isolation rules.",
        "delivered answer describes pod selectors or traffic isolation in "
        "non-error clauses",
    ),
    AcceptanceCriterion(
        "ac05-netpol-check-remains-read-only",
        "The NetworkPolicy check performs no mutations, leaks no internal "
        "LiteLLM proxy aliases, and finishes within the turn latency budget.",
        "complete normalized tool evidence contains no mutating calls, output "
        "contains no model-default or custom provider leak, and turn latency "
        "is within budget",
    ),
)

MILESTONES = (
    Milestone(
        INTERACTION_COMPLETED_MILESTONE,
        "The NetworkPolicy posture conversation finishes successfully.",
        "terminal completed interaction",
    ),
    Milestone(
        KAGE_DELEGATES_MILESTONE,
        "Kage delegates the NetworkPolicy inspection via kanban_create.",
        "root toolCalls contains a completed kanban_create",
        (INTERACTION_COMPLETED_MILESTONE,),
    ),
    Milestone(
        SPECIALIST_CREATED_MILESTONE,
        "A specialist task is created to inspect kubeagents-system "
        "NetworkPolicies.",
        "at least one projected specialist task exists",
        (KAGE_DELEGATES_MILESTONE,),
    ),
    Milestone(
        SPECIALIST_COMPLETES_MILESTONE,
        "The delegated specialist NetworkPolicy task finishes.",
        "at least one specialist task is done after at least one run",
        (SPECIALIST_CREATED_MILESTONE,),
    ),
    Milestone(
        READ_ONLY_MILESTONE,
        "Inspecting kubeagents-system NetworkPolicies mutates nothing.",
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
            "networkpolic(?:y|ies)",
            "netpols?",
            "polic(?:y|ies)",
        )
        if not UNRESTRICTED_NETPOL_CLAUSE_RE.search(clause)
        and not (
            re.search(r"\bunrestricted\b", clause, re.IGNORECASE)
            and not RESTRICTION_PREDICATE_RE.search(clause)
        )
    ]
    positive_text = "\n".join(positive_clauses)
    leaks = proxy_leaks(all_visible_text(interaction))
    operations = tool_operations(interaction)
    mutations = sorted(FORBIDDEN_OPERATIONS.intersection(operations))
    refusal = has_clarification_or_refusal(positive_text or answer)

    policies = sorted(
        {m.group(0).casefold() for m in NETPOL_NAME_RE.finditer(positive_text)}
    )
    directions = sorted(
        {
            m.group(0).casefold()
            for m in POLICY_DIRECTION_RE.finditer(positive_text)
        }
    )
    summary_text = PROMPT_PHRASE_SCRUB_RE.sub(
        " ", NETPOL_NAME_RE.sub(" ", positive_text)
    )
    has_summary = bool(ISOLATION_SUMMARY_RE.search(summary_text)) and not refusal

    suite = AcceptanceCriteria(ACCEPTANCE_CRITERIA)
    suite.record(
        "ac01-netpol-request-preserved",
        all(phrase in folded_input for phrase in REQUIRED_INPUT_PHRASES),
        raw_input,
    )
    suite.record(
        "ac02-kubeagents-network-policies-named",
        bool(policies) and not refusal,
        {"policies": policies, "answer": answer},
        blocked_by=output_blocker,
    )
    suite.record(
        "ac03-policy-direction-reported",
        bool(directions) and not refusal,
        {"directions": directions, "answer": answer},
        blocked_by=output_blocker,
    )
    suite.record(
        "ac04-isolation-posture-summarized",
        has_summary,
        {"hasIsolationSummary": has_summary, "answer": answer},
        blocked_by=output_blocker,
    )
    suite.record(
        "ac05-netpol-check-remains-read-only",
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


def test_09_network_policy_posture() -> None:
    run_harness_scenario(
        Scenario(
            SCENARIO_ID,
            build_prompt,
            evaluate_acceptance,
            evaluate_backend_milestones,
        )
    )
