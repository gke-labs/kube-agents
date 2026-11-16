"""Reproduce harness CUJ7: read-only kubeagents-system Service endpoints and port inventory."""

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

SCENARIO_ID = "cuj_harness_07"
READ_ONLY_MILESTONE = "m4-service-check-remains-read-only"
REQUIRED_INPUT_PHRASES = (
    "service",
    "kubeagents-system",
    "service types",
    "port numbers",
)

PROMPT = """Inspect the Kubernetes Service resources in the kubeagents-system \
namespace on this host cluster: (1) list the Service names in \
kubeagents-system, (2) report their Kubernetes Service types, and (3) report \
the exposed port numbers for each service. Include the kubeagents-system \
service names, service types, and port numbers in both the task completion \
summary and the full result. Do not create, modify, or delete any cluster \
resources."""

HARNESS_SERVICE_NAME_RE = re.compile(
    r"\b(?:platform-agent(?:-credential-proxy|-shell|-gateway)?(?!-[a-z0-9])"
    r"|(?<!the\s)litellm(?!-[a-z0-9])(?!\s+(?:backend|proxy|model|alias|config\w*|pods?)\b)"
    r"|inference-gateway(?!-[a-z0-9])(?!\s+pods?\b)"
    r"|kube-agents-(?:web-console(?:-public)?(?!-[a-z0-9])"
    r"|controller-manager-(?:metrics-)?service(?!-[a-z0-9])))\b",
    re.IGNORECASE,
)
POD_ONLY_CLAUSE_RE = re.compile(r"\bpods?\b", re.IGNORECASE)
SERVICE_CLAUSE_CONTEXT_RE = re.compile(
    r"\b(?:services?|svc|clusterip|loadbalancer|nodeport|headless|ports?|endpoints?)\b"
    r"|\b\d+/(?:tcp|udp)\b",
    re.IGNORECASE,
)
SERVICE_TYPE_RE = re.compile(
    r"\b(?:clusterip|loadbalancer|nodeport|headless)\b",
    re.IGNORECASE,
)
IPV4_OR_NON_PORT_NUMBER_RE = re.compile(
    r"\b\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?\b"
    r"|\b\d+(?:\.\d+)?%"
    r"|\b\d+\s+(?:pods?|nodes?|services?|containers?|events?|seconds?|minutes?"
    r"|hours?|days?|namespaces?|resources?|endpoints?|ms|s|m|h)\b",
    re.IGNORECASE,
)
PORT_CONTEXT_RE = re.compile(
    r"\b(?:ports?|expos\w*|listen\w*|targetport|tcp|udp|ssh|cred-proxy"
    r"|credential-proxy|dashboard)\b|->|\b\d+/(?:tcp|udp|\d+)\b",
    re.IGNORECASE,
)
SERVICE_PORT_RE = re.compile(
    r"(?<![.\d])\b(?:80|443|2222|4000|8080|8443|8642|8699|8765|9119)\b(?![.\d%])",
    re.IGNORECASE,
)

ACCEPTANCE_CRITERIA = (
    AcceptanceCriterion(
        "ac01-service-request-preserved",
        "The request asks for Service names, service types, and port numbers "
        "in kubeagents-system.",
        "projected user input names services, kubeagents-system, service "
        "types, and port numbers",
    ),
    AcceptanceCriterion(
        "ac02-kubeagents-services-named",
        "The answer names the Kubernetes Services running in kubeagents-system.",
        "delivered answer names at least two kubeagents-system Services",
    ),
    AcceptanceCriterion(
        "ac03-service-types-reported",
        "The answer reports the Kubernetes Service types (ClusterIP or "
        "LoadBalancer).",
        "delivered answer reports ClusterIP or LoadBalancer service types",
    ),
    AcceptanceCriterion(
        "ac04-service-ports-reported",
        "The answer reports the exposed Service port numbers in "
        "kubeagents-system.",
        "delivered answer includes exposed harness service ports (such as "
        "8642, 4000, 80, or 8080)",
    ),
    AcceptanceCriterion(
        "ac05-service-check-remains-read-only",
        "The Service endpoint check performs no mutations, leaks no internal "
        "LiteLLM proxy aliases, and finishes within the turn latency budget.",
        "complete normalized tool evidence contains no mutating calls, output "
        "contains no model-default or custom provider leak, and turn latency "
        "is within budget",
    ),
)

MILESTONES = (
    Milestone(
        INTERACTION_COMPLETED_MILESTONE,
        "The kubeagents-system Service endpoint conversation finishes "
        "successfully.",
        "terminal completed interaction",
    ),
    Milestone(
        KAGE_DELEGATES_MILESTONE,
        "Kage delegates the Service inspection via kanban_create.",
        "root toolCalls contains a completed kanban_create",
        (INTERACTION_COMPLETED_MILESTONE,),
    ),
    Milestone(
        SPECIALIST_CREATED_MILESTONE,
        "A specialist task is created to inspect kubeagents-system Services.",
        "at least one projected specialist task exists",
        (KAGE_DELEGATES_MILESTONE,),
    ),
    Milestone(
        SPECIALIST_COMPLETES_MILESTONE,
        "The delegated specialist Service task finishes.",
        "at least one specialist task is done after at least one run",
        (SPECIALIST_CREATED_MILESTONE,),
    ),
    Milestone(
        READ_ONLY_MILESTONE,
        "Inspecting kubeagents-system Services mutates nothing.",
        "complete normalized tool evidence contains no forbidden mutation",
        (INTERACTION_COMPLETED_MILESTONE,),
    ),
)


def _reported_ports(clauses: list[str]) -> list[str]:
    found: set[str] = set()
    header_port_context = False
    for clause in clauses:
        scrubbed = IPV4_OR_NON_PORT_NUMBER_RE.sub(" ", clause)
        has_local_port_ctx = bool(PORT_CONTEXT_RE.search(scrubbed))
        if has_local_port_ctx:
            header_port_context = True
        has_service_row_ctx = bool(
            HARNESS_SERVICE_NAME_RE.search(scrubbed)
            and (SERVICE_TYPE_RE.search(scrubbed) or header_port_context)
        )
        if not (has_local_port_ctx or has_service_row_ctx):
            continue
        for match in SERVICE_PORT_RE.finditer(scrubbed):
            found.add(match.group(0))
    return sorted(found)


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
            "services?",
            "svc",
            "endpoints?",
        )
        if not (
            POD_ONLY_CLAUSE_RE.search(clause)
            and not SERVICE_CLAUSE_CONTEXT_RE.search(clause)
        )
    ]
    positive_text = "\n".join(positive_clauses)
    leaks = proxy_leaks(all_visible_text(interaction))
    operations = tool_operations(interaction)
    mutations = sorted(FORBIDDEN_OPERATIONS.intersection(operations))
    refusal = has_clarification_or_refusal(positive_text or answer)

    services = sorted(
        {
            m.group(0).casefold()
            for m in HARNESS_SERVICE_NAME_RE.finditer(positive_text)
        }
    )
    service_types = sorted(
        {m.group(0).casefold() for m in SERVICE_TYPE_RE.finditer(positive_text)}
    )
    ports = _reported_ports(positive_clauses)

    suite = AcceptanceCriteria(ACCEPTANCE_CRITERIA)
    suite.record(
        "ac01-service-request-preserved",
        all(phrase in folded_input for phrase in REQUIRED_INPUT_PHRASES),
        raw_input,
    )
    suite.record(
        "ac02-kubeagents-services-named",
        len(services) >= 2 and not refusal,
        {"services": services, "answer": answer},
        blocked_by=output_blocker,
    )
    suite.record(
        "ac03-service-types-reported",
        bool(service_types) and not refusal,
        {"serviceTypes": service_types, "answer": answer},
        blocked_by=output_blocker,
    )
    suite.record(
        "ac04-service-ports-reported",
        bool(ports) and not refusal,
        {"ports": ports, "answer": answer},
        blocked_by=output_blocker,
    )
    suite.record(
        "ac05-service-check-remains-read-only",
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


def test_07_kubeagents_service_endpoints() -> None:
    run_harness_scenario(
        Scenario(
            SCENARIO_ID,
            build_prompt,
            evaluate_acceptance,
            evaluate_backend_milestones,
        )
    )
