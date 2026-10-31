"""Reproduce harness CUJ3: read-only inventory of kubeagents-system pods, services, CRDs, and nodes."""

from __future__ import annotations

import re
from typing import Any

from cuj.harness.common import (
    FORBIDDEN_OPERATIONS,
    INTERACTION_COMPLETED_MILESTONE,
    KAGE_DELEGATES_MILESTONE,
    NEGATED_OR_ERROR_CLAUSE_RE,
    SPECIALIST_COMPLETES_MILESTONE,
    SPECIALIST_CREATED_MILESTONE,
    all_visible_text,
    answer_text,
    has_positive_node_ready,
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

SCENARIO_ID = "cuj_harness_03"
HARNESS_NAMESPACE = "kubeagents-system"
HARNESS_CRD_GROUP = "kubeagents.x-k8s.io"
READ_ONLY_MILESTONE = "m4-inventory-remains-read-only"
REQUIRED_INPUT_PHRASES = (
    HARNESS_NAMESPACE,
    "running pods",
    "services",
    HARNESS_CRD_GROUP,
    "node readiness",
)

PROMPT = """Perform a read-only Kubernetes inventory of this host cluster's \
kube-agents harness installation: (1) list the active cluster namespaces \
(confirming kubeagents-system) and report cluster node readiness and the \
Kubernetes server version (v1.x), (2) list the running pods and exposed \
services in the kubeagents-system namespace, and (3) list the installed \
kubeagents.x-k8s.io Custom Resource Definitions (CRDs). Include the \
kubeagents-system pods, services, kubeagents.x-k8s.io CRDs, node Ready \
status, and v1.x version in both the task completion summary and the full \
result. Do not create, modify, or delete any cluster resources."""

_NON_WORKLOAD_SUFFIX = (
    r"(?!-(?:config|service|svc|netpol|policy|pvc|secret|sa|role|binding"
    r"|public|host|metrics|monitoring|gw-chat-metrics|cred-metrics"
    r"|a2a-metrics|mon-netpol|ingress|egress)\b)"
)
HARNESS_WORKLOAD_RE = re.compile(
    rf"\b(?:"
    rf"platform-?agent-(?:gateway|shell|credential-proxy|sandbox){_NON_WORKLOAD_SUFFIX}(?:-[a-z0-9-]+)?"
    rf"|inference-gateway{_NON_WORKLOAD_SUFFIX}(?:-[a-z0-9-]+)?"
    rf"|litellm{_NON_WORKLOAD_SUFFIX}(?:-[a-z0-9-]+)?"
    rf"|kube-agents-(?:web-console|controller-manager){_NON_WORKLOAD_SUFFIX}(?:-[a-z0-9-]+)?"
    rf")\b",
    re.IGNORECASE,
)
SHARED_SERVICE_AND_WORKLOAD_NAMES = frozenset(
    {
        "inference-gateway",
        "litellm",
        "kube-agents-web-console",
    }
)
WORKLOAD_CONTEXT_RE = re.compile(
    r"\b(?:pods?|workloads?|deployments?|statefulsets?|replicasets?|containers?)\b"
    r"|\b\d+\s*/\s*\d+\s*running\b",
    re.IGNORECASE,
)
NON_POD_SERVICE_LABEL_RE = re.compile(
    r"\b(?:services?|svc|network\s*polic(?:y|ies)|netpols?|configmaps?|secrets?)\s+"
    r"(?:running|present|exposed|configured)\s+in\b",
    re.IGNORECASE,
)
SERVICE_RE = re.compile(
    r"\b(?:clusterip|loadbalancer|nodeport|headless|port\s+\d+|\d+/tcp)\b"
    r"|\b(?:services?|svc)\b[^.\n;]{0,80}\b(?:platform-agent|inference-gateway"
    r"|litellm|kube-agents-web-console|kube-agents-controller-manager)\b"
    r"|\b(?:platform-agent|inference-gateway|litellm|kube-agents-web-console"
    r"|kube-agents-controller-manager)\s+(?:services?|svc)\b",
    re.IGNORECASE,
)
CRD_RE = re.compile(
    r"\b(?:platformagents|agentprofiles|agentplugins)\.kubeagents\.x-k8s\.io\b",
    re.IGNORECASE,
)
PAREN_GROUP_RE = re.compile(r"\([^)]*\)")
NON_CRD_INSTANCE_ABSENCE_RE = re.compile(
    r"\b(?:with\s+)?(?:no|(?<!-)0|zero|without(?:\s+any)?)\s+"
    r"(?:[a-z0-9_.-]+\s+){0,2}?"
    r"(?:instances?|objects?|resources?|crs?|custom\s+resources?"
    r"|issues?|errors?|problems?|warnings?)"
    r"(?:\s+(?:exist\w*|found|present|created|deployed|configured|yet))?\b",
    re.IGNORECASE,
)
NEGATED_CRD_SEGMENT_RE = re.compile(
    r"\b(?:no(?!\s+(?:issues?|errors?|problems?|warnings?)\b)"
    r"|not|none|never|neither|without|lacks?|missing|absent"
    r"|unavailable|removed|deleted|pending|failed"
    r"|failed\s+to\s+(?:find|list|get)|unable\s+to\s+(?:find|list|get)"
    r"|0\s+(?:installed\s+)?crds?)\b",
    re.IGNORECASE,
)
NEGATED_CRD_POST_RE = re.compile(
    r"\b(?:no|not|none|never|neither|without|missing|absent|lacks?"
    r"|uninstalled|unregistered|notfound|unavailable|removed|deleted"
    r"|pending|failed)\b|n't\b",
    re.IGNORECASE,
)
K8S_VERSION_RE = re.compile(
    r"\bv1\.\d+(?:\.\d+)?(?:-[a-z0-9.+_-]+)?\b",
    re.IGNORECASE,
)

ACCEPTANCE_CRITERIA = (
    AcceptanceCriterion(
        "ac01-inventory-request-preserved",
        "The request asks for kubeagents-system pods and services, "
        "kubeagents.x-k8s.io CRDs, and node readiness and version.",
        "projected user input names kubeagents-system, pods, services, "
        "kubeagents.x-k8s.io CRDs, and node readiness",
    ),
    AcceptanceCriterion(
        "ac02-kubeagents-system-pods-and-services-reported",
        "The answer inventories running pods and exposed services in the "
        "kubeagents-system namespace.",
        "delivered answer names kubeagents-system, running harness workloads, "
        "and exposed services in non-negated clauses",
    ),
    AcceptanceCriterion(
        "ac03-kubeagents-crds-reported",
        "The answer lists the installed kube-agents Custom Resource Definitions.",
        "delivered answer names installed *.kubeagents.x-k8s.io CRDs",
    ),
    AcceptanceCriterion(
        "ac04-node-readiness-and-version-reported",
        "The answer reports cluster node readiness and the Kubernetes server "
        "version.",
        "delivered answer reports positive node Ready status and a v1.x "
        "Kubernetes version",
    ),
    AcceptanceCriterion(
        "ac05-inventory-remains-read-only",
        "The inventory check performs no mutations, leaks no internal "
        "LiteLLM proxy aliases, and finishes within the turn latency budget.",
        "complete normalized tool evidence contains no mutating calls, output "
        "contains no model-default or custom provider leak, and turn latency "
        "is within budget",
    ),
)

MILESTONES = (
    Milestone(
        INTERACTION_COMPLETED_MILESTONE,
        "The harness namespace and CRD inventory conversation finishes "
        "successfully.",
        "terminal completed interaction",
    ),
    Milestone(
        KAGE_DELEGATES_MILESTONE,
        "Kage delegates the cluster and harness inventory via kanban_create.",
        "root toolCalls contains a completed kanban_create",
        (INTERACTION_COMPLETED_MILESTONE,),
    ),
    Milestone(
        SPECIALIST_CREATED_MILESTONE,
        "A specialist task is created to inspect the harness namespace and CRDs.",
        "at least one projected specialist task exists",
        (KAGE_DELEGATES_MILESTONE,),
    ),
    Milestone(
        SPECIALIST_COMPLETES_MILESTONE,
        "The delegated specialist inventory task finishes.",
        "at least one specialist task is done after at least one run",
        (SPECIALIST_CREATED_MILESTONE,),
    ),
    Milestone(
        READ_ONLY_MILESTONE,
        "Inspecting the harness namespace, CRDs, and nodes mutates nothing.",
        "complete normalized tool evidence contains no forbidden mutation",
        (INTERACTION_COMPLETED_MILESTONE,),
    ),
)


def _strip_non_crd_parens(text: str) -> str:
    return PAREN_GROUP_RE.sub(
        lambda m: f" {m.group(0)[1:-1]} "
        if CRD_RE.search(m.group(0))
        else " ",
        text,
    )


def _reported_crds(clauses: list[str]) -> list[str]:
    found: set[str] = set()
    for raw_clause in clauses:
        if NEGATED_OR_ERROR_CLAUSE_RE.search(raw_clause):
            continue
        clause = NON_CRD_INSTANCE_ABSENCE_RE.sub(
            " ",
            _strip_non_crd_parens(raw_clause).replace("\u2019", "'"),
        )
        all_matches = list(CRD_RE.finditer(clause))
        if not all_matches:
            continue
        shared_tail = clause[all_matches[-1].end() :]
        shared_tail_negated = bool(NEGATED_CRD_POST_RE.search(shared_tail))
        negated_list_active = False
        for segment in clause.split(","):
            seg_matches = list(CRD_RE.finditer(segment))
            if not seg_matches:
                negated_list_active = False
                continue
            first_pre = segment[: seg_matches[0].start()].rsplit(":", 1)[-1]
            if NEGATED_CRD_SEGMENT_RE.search(first_pre):
                negated_list_active = True
            for idx, match in enumerate(seg_matches):
                prev_end = seg_matches[idx - 1].end() if idx > 0 else 0
                pre_text = segment[prev_end : match.start()].rsplit(":", 1)[-1]
                post_start = match.end()
                post_end = (
                    seg_matches[idx + 1].start()
                    if idx + 1 < len(seg_matches)
                    else len(segment)
                )
                post_text = segment[post_start:post_end]
                inherits_negated_tail = (
                    not post_text.strip() and shared_tail_negated
                )
                if (
                    negated_list_active
                    or NEGATED_CRD_SEGMENT_RE.search(pre_text)
                    or NEGATED_CRD_POST_RE.search(post_text)
                    or inherits_negated_tail
                ):
                    continue
                found.add(match.group(0).casefold())
    return sorted(found)


def _positive_workloads_and_services(
    answer: str,
    raw_input: str,
) -> tuple[bool, list[str], bool]:
    positive_clauses = positive_inventory_clauses(
        answer,
        raw_input,
        "pods?",
        "workloads?",
        "services?",
        "svc",
    )
    positive_text = "\n".join(positive_clauses)
    has_namespace = HARNESS_NAMESPACE in positive_text.casefold()
    workload_set: set[str] = set()
    for clause in positive_clauses:
        has_workload_ctx = bool(
            WORKLOAD_CONTEXT_RE.search(clause)
        ) and not bool(NON_POD_SERVICE_LABEL_RE.search(clause))
        for match in HARNESS_WORKLOAD_RE.finditer(clause):
            token = match.group(0).casefold()
            if (
                token in SHARED_SERVICE_AND_WORKLOAD_NAMES
                and not has_workload_ctx
            ):
                continue
            workload_set.add(token)
    workloads = sorted(workload_set)
    has_services = bool(SERVICE_RE.search(positive_text))
    return has_namespace, workloads, has_services


def evaluate_acceptance(interaction: dict[str, Any]) -> AcceptanceCriteria:
    _, output_blocker = interaction_blockers(interaction)
    ro_blockers = read_only_blockers(interaction)
    latency_ok, latency_s, budget_s = within_latency_budget(interaction)

    raw_input = input_text(interaction)
    folded_input = raw_input.casefold()

    answer = answer_text(interaction)
    clauses = non_echo_clauses(answer, raw_input)
    non_echo_text = "\n".join(clauses)
    leaks = proxy_leaks(all_visible_text(interaction))
    operations = tool_operations(interaction)
    mutations = sorted(FORBIDDEN_OPERATIONS.intersection(operations))

    has_namespace, workloads, has_services = _positive_workloads_and_services(
        answer, raw_input
    )
    crds = _reported_crds(clauses)
    versions = sorted(
        {
            match.group(0).casefold()
            for match in K8S_VERSION_RE.finditer(non_echo_text)
        }
    )
    node_ready = has_positive_node_ready(clauses)

    suite = AcceptanceCriteria(ACCEPTANCE_CRITERIA)
    suite.record(
        "ac01-inventory-request-preserved",
        all(phrase in folded_input for phrase in REQUIRED_INPUT_PHRASES),
        raw_input,
    )
    suite.record(
        "ac02-kubeagents-system-pods-and-services-reported",
        has_namespace and bool(workloads) and has_services,
        {
            "hasNamespace": has_namespace,
            "workloads": workloads,
            "hasServices": has_services,
            "answer": answer,
        },
        blocked_by=output_blocker,
    )
    suite.record(
        "ac03-kubeagents-crds-reported",
        bool(crds),
        {"crds": crds, "answer": answer},
        blocked_by=output_blocker,
    )
    suite.record(
        "ac04-node-readiness-and-version-reported",
        node_ready and bool(versions),
        {
            "nodeReady": node_ready,
            "versions": versions,
            "answer": answer,
        },
        blocked_by=output_blocker,
    )
    suite.record(
        "ac05-inventory-remains-read-only",
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


def test_03_harness_namespace_and_crd_inventory() -> None:
    run_harness_scenario(
        Scenario(
            SCENARIO_ID,
            build_prompt,
            evaluate_acceptance,
            evaluate_backend_milestones,
        )
    )
