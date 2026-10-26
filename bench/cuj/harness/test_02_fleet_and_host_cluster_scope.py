"""Reproduce harness CUJ2: fleet and host cluster GCP project and region scope."""

from __future__ import annotations

import os
import re
from typing import Any

from cuj.harness.common import (
    CLUSTER_NAME_ENV,
    FORBIDDEN_OPERATIONS,
    INTERACTION_COMPLETED_MILESTONE,
    KAGE_DELEGATES_MILESTONE,
    PROJECT_ID_ENV,
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
from cuj.utils.scenario import Scenario, required_env

SCENARIO_ID = "cuj_harness_02"
READ_ONLY_MILESTONE = "m4-scope-check-remains-read-only"
MIN_SCOPE_ANSWER_CHARS = 20
REQUIRED_INPUT_PHRASES = (
    "gke clusters",
    "host cluster",
    "region",
)

PROMPT = """Inspect the fleet and host cluster scope for this kube-agents \
installation in project {project_id}: (1) confirm the GCP projects currently \
in scope, (2) list the GKE clusters visible across the fleet in {project_id}, \
and (3) identify the host cluster name, its GCP project, and its GCP region \
or location. Include the in-scope project ID, visible fleet clusters, host \
cluster name, and GCP region in both the task completion summary and the full \
result. This is a read-only scope check: do not create, modify, or delete any \
resources."""

GCP_LOCATION_RE = re.compile(
    r"(?<![a-z0-9-])"
    r"(?:us|europe|asia|northamerica|southamerica|australia|me|africa)"
    r"-[a-z]+\d+(?:-[a-z])?"
    r"(?![a-z0-9-])",
    re.IGNORECASE,
)
CLUSTER_SCOPE_RE = re.compile(r"\b(?:clusters?|fleet|host)\b", re.IGNORECASE)
AGENT_PROFILE_REF_RE = re.compile(
    r"\b(?:agent\s+)?profile\s+[`'\"]?cluster-[a-z0-9-]+[`'\"]?\b"
    r"|\bcluster-[a-z0-9-]+-(?:us|europe|asia|northamerica|southamerica|australia|me|africa)-[a-z]+\d+(?:-[a-z])?\b",
    re.IGNORECASE,
)
KNOWN_CLUSTER_NAME_RE = re.compile(
    r"\b(?:platform-agent-host|cymbal-shops|openrl-gpu-hub"
    r"|ka-webconsole-[a-z0-9-]+|gke-[a-z0-9-]+"
    r"|[a-z0-9]+(?:-[a-z0-9]+)*-cluster(?:-[a-z0-9-]+)?"
    r"|cluster-[a-z0-9-]+)\b",
    re.IGNORECASE,
)
NON_CLUSTER_TOKEN_RE = re.compile(
    r"^(?:(?:single|multi|in|per|cross|host|target|same|inter|intra|sub|all"
    r"|any|non|whole|full|every|each|other|local|remote|fleet|cloud"
    r"|control-plane|node-pool|out-of-scope|in-scope)-cluster"
    r"|cluster-(?:wide|level|scoped?|admin|role|rolebinding|ip|ips"
    r"|autoscaler|info|cidr|dns|domain|network|mode|status|state"
    r"|scope|config|configuration|name|names|id|ids|type|types)"
    r"|gke-gcloud-auth-plugin"
    r"|gke-[a-z0-9-]+-pool-[a-z0-9-]+)$",
    re.IGNORECASE,
)
ADJACENT_CLUSTER_NAME_RE = re.compile(
    r"\b(?:host\s+)?clusters?(?:\s+names?)?"
    r"(?:(?:\s+(?:named|called)\s+|\s*[:=]\s*)[`'\"]?([a-z][a-z0-9_-]{2,62})[`'\"]?"
    r"|\s+(?:is|are|include|includes)\s+(?:[`'\"]([a-z][a-z0-9_-]{2,62})[`'\"]|([a-z0-9]+-[a-z0-9_-]+)))\b"
    r"|(?:[`'\"]([a-z][a-z0-9_-]{2,62})[`'\"]|\b([a-z0-9]+-[a-z0-9_-]+))\s*"
    r"(?:\(\s*host\s+cluster\s*\)|\s+is\s+the\s+host\s+cluster\b)",
    re.IGNORECASE,
)
CLUSTER_STOPWORDS = frozenset(
    {
        "across",
        "active",
        "all",
        "and",
        "are",
        "called",
        "check",
        "checked",
        "confirm",
        "confirmed",
        "control-plane",
        "current",
        "currently",
        "discovered",
        "exist",
        "exists",
        "fleet",
        "for",
        "found",
        "from",
        "gcp",
        "gke",
        "has",
        "have",
        "host",
        "identified",
        "identify",
        "in-scope",
        "include",
        "includes",
        "inspected",
        "into",
        "is",
        "kube-agents",
        "kubeagents-system",
        "list",
        "listed",
        "located",
        "location",
        "multi-cluster",
        "name",
        "named",
        "node-pool",
        "none",
        "non-error",
        "non-zero",
        "only",
        "out-of-scope",
        "plan-and-delegate",
        "present",
        "project",
        "projects",
        "read-only",
        "region",
        "reported",
        "running",
        "scope",
        "single",
        "that",
        "the",
        "their",
        "there",
        "these",
        "this",
        "those",
        "two",
        "three",
        "uses",
        "using",
        "visible",
        "was",
        "were",
        "which",
        "with",
        "within",
        "workload-identity",
    }
)

ACCEPTANCE_CRITERIA = (
    AcceptanceCriterion(
        "ac01-scope-request-preserved",
        "The request asks for in-scope GCP projects, visible GKE fleet "
        "clusters, and the host cluster project and region.",
        "projected user input names the target project ID, fleet clusters, "
        "host cluster, and region",
    ),
    AcceptanceCriterion(
        "ac02-in-scope-project-confirmed",
        "The answer confirms the target GCP project is in scope.",
        "delivered answer names the configured GCP project ID on hyphen-aware "
        "word boundaries in a non-error clause",
    ),
    AcceptanceCriterion(
        "ac03-fleet-and-host-clusters-reported",
        "The answer reports the visible GKE fleet clusters and identifies the "
        "host cluster.",
        "delivered answer names at least one concrete GKE cluster identifier "
        "in a non-error fleet/host scope clause without asking which cluster "
        "was meant",
    ),
    AcceptanceCriterion(
        "ac04-host-cluster-region-reported",
        "The answer reports the GCP region or zone where the host cluster is "
        "located.",
        "delivered answer contains a valid standalone GCP region or zone "
        "identifier in a non-error clause",
    ),
    AcceptanceCriterion(
        "ac05-scope-check-remains-read-only",
        "The fleet and host scope check performs no mutations, leaks no "
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
        "The fleet and host cluster scope conversation finishes successfully.",
        "terminal completed interaction",
    ),
    Milestone(
        KAGE_DELEGATES_MILESTONE,
        "Kage delegates the fleet and host cluster scope inspection via "
        "kanban_create.",
        "root toolCalls contains a completed kanban_create",
        (INTERACTION_COMPLETED_MILESTONE,),
    ),
    Milestone(
        SPECIALIST_CREATED_MILESTONE,
        "A specialist task is created to inspect fleet and host cluster scope.",
        "at least one projected specialist task exists",
        (KAGE_DELEGATES_MILESTONE,),
    ),
    Milestone(
        SPECIALIST_COMPLETES_MILESTONE,
        "The delegated specialist scope inspection finishes.",
        "at least one specialist task is done after at least one run",
        (SPECIALIST_CREATED_MILESTONE,),
    ),
    Milestone(
        READ_ONLY_MILESTONE,
        "Inspecting fleet and host cluster scope mutates nothing.",
        "complete normalized tool evidence contains no forbidden mutation",
        (INTERACTION_COMPLETED_MILESTONE,),
    ),
)


def _reported_clusters(
    clauses: list[str],
    project_re: re.Pattern[str] | None,
    configured_cluster: str = "",
) -> list[str]:
    configured_re = (
        re.compile(
            rf"(?<![a-z0-9-]){re.escape(configured_cluster)}(?![a-z0-9-])",
            re.IGNORECASE,
        )
        if configured_cluster
        else None
    )
    found: set[str] = set()
    for clause in clauses:
        if not CLUSTER_SCOPE_RE.search(clause):
            continue
        cleaned = AGENT_PROFILE_REF_RE.sub(" ", clause)
        if project_re is not None:
            cleaned = project_re.sub(" ", cleaned)
        cleaned = GCP_LOCATION_RE.sub(" ", cleaned)
        if configured_re is not None and configured_re.search(cleaned):
            found.add(configured_cluster.casefold())
        for match in KNOWN_CLUSTER_NAME_RE.finditer(cleaned):
            token = match.group(0).casefold()
            if (
                token not in CLUSTER_STOPWORDS
                and not NON_CLUSTER_TOKEN_RE.search(token)
            ):
                found.add(token)
        for match in ADJACENT_CLUSTER_NAME_RE.finditer(cleaned):
            raw_token = next((g for g in match.groups() if g), "")
            token = raw_token.casefold()
            if (
                token
                and token not in CLUSTER_STOPWORDS
                and not NON_CLUSTER_TOKEN_RE.search(token)
            ):
                found.add(token)
    return sorted(found)


def evaluate_acceptance(interaction: dict[str, Any]) -> AcceptanceCriteria:
    _, output_blocker = interaction_blockers(interaction)
    ro_blockers = read_only_blockers(interaction, require_tasks=False)
    latency_ok, latency_s, budget_s = within_latency_budget(interaction)

    raw_input = input_text(interaction)
    folded_input = raw_input.casefold()
    project_id = os.environ.get(PROJECT_ID_ENV, "").strip()
    configured_cluster = os.environ.get(CLUSTER_NAME_ENV, "").strip()

    answer = answer_text(interaction)
    leaks = proxy_leaks(all_visible_text(interaction))
    operations = tool_operations(interaction)
    mutations = sorted(FORBIDDEN_OPERATIONS.intersection(operations))

    positive_clauses = positive_inventory_clauses(
        answer,
        raw_input,
        "clusters?",
        "fleet",
    )
    positive_text = "\n".join(positive_clauses)
    scrubbed_positive = AGENT_PROFILE_REF_RE.sub("", positive_text)

    project_re = (
        re.compile(
            rf"(?<![a-z0-9-]){re.escape(project_id)}(?![a-z0-9-])",
            re.IGNORECASE,
        )
        if project_id
        else None
    )
    project_confirmed = bool(
        project_re and project_re.search(scrubbed_positive)
    )
    clarification_loop = has_clarification_or_refusal(answer)
    clusters = _reported_clusters(
        positive_clauses, project_re, configured_cluster
    )
    expected_cluster_ok = (
        not configured_cluster
        or configured_cluster.casefold() in clusters
    )
    clusters_reported = (
        bool(clusters)
        and expected_cluster_ok
        and not clarification_loop
        and len(positive_text.strip()) >= MIN_SCOPE_ANSWER_CHARS
    )
    location_source = (
        project_re.sub("", scrubbed_positive)
        if project_re
        else scrubbed_positive
    )
    locations = sorted(
        {
            match.group(0).casefold()
            for match in GCP_LOCATION_RE.finditer(location_source)
        }
    )

    suite = AcceptanceCriteria(ACCEPTANCE_CRITERIA)
    suite.record(
        "ac01-scope-request-preserved",
        bool(project_id)
        and project_id.casefold() in folded_input
        and all(phrase in folded_input for phrase in REQUIRED_INPUT_PHRASES),
        raw_input,
    )
    suite.record(
        "ac02-in-scope-project-confirmed",
        project_confirmed,
        {"expectedProjectId": project_id, "answer": answer},
        blocked_by=output_blocker,
    )
    suite.record(
        "ac03-fleet-and-host-clusters-reported",
        clusters_reported,
        {
            "clusters": clusters,
            "clarificationLoop": clarification_loop,
            "answer": answer,
        },
        blocked_by=output_blocker,
    )
    suite.record(
        "ac04-host-cluster-region-reported",
        bool(locations),
        {"locations": locations, "answer": answer},
        blocked_by=output_blocker,
    )
    suite.record(
        "ac05-scope-check-remains-read-only",
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
        require_tasks=False,
    )
    return suite


def build_prompt() -> str:
    return PROMPT.format(project_id=required_env(PROJECT_ID_ENV))


def test_02_fleet_and_host_cluster_scope() -> None:
    run_harness_scenario(
        Scenario(
            SCENARIO_ID,
            build_prompt,
            evaluate_acceptance,
            evaluate_backend_milestones,
        )
    )
