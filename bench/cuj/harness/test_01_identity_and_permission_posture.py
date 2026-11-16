"""Reproduce harness CUJ1: backing model identity, coordinator role, and permission posture."""

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
    input_text,
    interaction_blockers,
    non_echo_clauses,
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

SCENARIO_ID = "cuj_harness_01"
NO_PROXY_LEAK_MILESTONE = "m4-no-internal-proxy-leak"
READ_ONLY_MILESTONE = "m5-inspection-remains-read-only"
REQUIRED_INPUT_PHRASES = (
    "backing llm model",
    "front-door role",
    "which tools",
    "cli commands",
    "permission posture",
)

PROMPT = """Report the harness identity and permission posture for this \
kube-agents installation by delegating an inspection of the litellm-config \
ConfigMap in the kubeagents-system namespace to the platform specialist: \
(1) state the upstream backing LLM model and cloud provider serving inference \
from litellm_params.model in litellm-config (state only the upstream provider \
and model name, and omit any internal router alias names), (2) state the \
conversational front-door role and how it routes work to other agents, \
(3) state which tools the front door holds versus how kubectl and gcloud CLI \
commands are executed, and (4) state the front-door permission posture \
regarding cluster or cloud infrastructure changes. Include all four items in \
both the task completion summary and the full result. Do not mutate any \
cluster or cloud state."""

BACKING_MODEL_RE = re.compile(
    r"\b(?:"
    r"gemini[-_./][a-z0-9._-]+"
    r"|gemini\s+\d+(?:\.\d+)?(?:\s+(?:pro|flash|ultra|nano))?"
    r"|claude[-_./][a-z0-9._-]+"
    r"|claude\s+(?:sonnet|opus|haiku|[34](?:\.\d+)?)(?:\s+\d+(?:\.\d+)?)?"
    r"|gpt[-_./][a-z0-9._-]+"
    r"|o[134](?:-[a-z0-9._-]+)?"
    r"|llama[-_./][a-z0-9._-]+"
    r"|deepseek[-_./][a-z0-9._-]+"
    r"|qwen[-_./a-z0-9.]+"
    r")\b",
    re.IGNORECASE,
)
BACKING_PROVIDER_RE = re.compile(
    r"\b(?:vertex(?:[-_ ]ai)?|google(?:\s+cloud)?|anthropic"
    r"|openai(?:[-_ ]compatible)?|sap)\b",
    re.IGNORECASE,
)
COORDINATOR_ROLE_RE = re.compile(
    r"\b(?:coordinator|planning\s+agent|orchestrat\w*)\b",
    re.IGNORECASE,
)
DELEGATION_RE = re.compile(
    r"\b(?:delegat\w*|kanban(?:_create)?|specialist\s+agents?)\b",
    re.IGNORECASE,
)
NEGATED_POSTURE_CLAUSE_RE = re.compile(
    r"\b(?:do\s+not|don't|does\s+not|doesn't|did\s+not|didn't|is\s+not|isn't"
    r"|are\s+not|aren't|am\s+not|have\s+no|has\s+no|had\s+no"
    r"|there\s+(?:is|are)\s+no|with\s+no|not|never|neither|without|no|none)\s+"
    r"(?:(?:operat(?:e|es|ing)|act(?:s|ing)?|run(?:s|ning)?|work(?:s|ing)?"
    r"|function(?:s|ing)?|serv(?:e|es|ing)|us(?:e|es|ing)|delegat(?:e|es|ing)"
    r"|rout(?:e|es|ing))\s+(?:in\s+|as\s+|via\s+|to\s+)?)?"
    r"(?:(?:a|an|the|any|strict|strictly)\s+){0,2}"
    r"\b(?:read[- ]only|plan[- ]and[- ]delegate|plan[- ]only|non[- ]mutating"
    r"|coordinator|planning\s+agent|orchestrat\w*"
    r"|delegat\w*|kanban(?:_create)?|list_agents|specialist\s+agents?)\b",
    re.IGNORECASE,
)
POSTURE_CANNOT_RE = re.compile(
    r"\b(?:cannot|can't|do\s+not|don't|does\s+not|doesn't)\s+"
    r"(?:directly\s+)?(?:mutate|modify|change|alter|apply|delete|patch"
    r"|run\s+(?:kubectl|gcloud)|execute\s+(?:kubectl|gcloud)"
    r"|have\s+direct\s+(?:kubectl|gcloud|cli|access|cluster|cloud))\b",
    re.IGNORECASE,
)
UNCONFIRMED_MODEL_CLAUSE_RE = re.compile(
    r"\b(?:whether|if)\s+(?:the\s+)?(?:backing\s+|upstream\s+)?(?:llm\s+)?(?:model|provider)\b"
    r"|\b(?:model|provider)\s+(?:is|was)\s+(?:unknown|unconfirmed|unavailable|not\s+confirmed)\b",
    re.IGNORECASE,
)
PERMISSION_POSTURE_RE = re.compile(
    r"\b(?:plan[- ]and[- ]delegate|plan[- ]only"
    r"|read[- ]only(?!\s+(?:check|inspection|audit|query|lookup|test|task)\b)"
    r"|non[- ]mutating(?:\s+posture)?"
    r"|no\s+(?:direct\s+)?(?:cluster|cloud|kubernetes)(?:\s+or\s+cloud)?\s+"
    r"(?:credentials|mutation\s+permissions)"
    r"|zero\s+(?:direct\s+)?(?:mutation|infrastructure\s+permissions)"
    r"|unprivileged\s+conversational\s+router)\b",
    re.IGNORECASE,
)
NO_DIRECT_CLI_RE = re.compile(
    r"\b(?:(?:no|without)\s+direct\s+(?:kubectl|gcloud|cli|cluster|cloud"
    r"|infrastructure|terminal|shell|write|mutation|access|tools|credentials)"
    r"|cannot\s+(?:mutate|modify|run\s+kubectl)"
    r"|do\s+not\s+(?:run\s+kubectl|have\s+direct\s+(?:kubectl|gcloud|cli|access))"
    r"|don't\s+(?:run\s+kubectl|have\s+direct\s+(?:kubectl|gcloud|cli|access))"
    r"|no\s+infrastructure\s+tools"
    r"|no\s+write\s+(?:access|permissions?|tools?|capabilities)"
    r"|no\s+(?:local\s+)?(?:terminal|kubectl|gcloud|credentials)"
    r"|holds?\s+only\b|only\s+(?:holds?\s+)?(?:kanban_create|list_agents))\b",
    re.IGNORECASE,
)
DIRECT_MUTATION_CLAIM_RE = re.compile(
    r"(?<!not\s)(?<!cannot\s)(?<!never\s)\b(?:"
    r"(?:i|we|the\s+front[- ]door(?:\s+agent|\s+coordinator)?|kage)\s+"
    r"(?:(?:am|are|is)\s+)?(?:can|able\s+to)\s+(?:also\s+|directly\s+|freely\s+)*"
    r"(?:(?:run|execute|invoke|perform|issue)\s+)?(?:kubectl\s+)?"
    r"(?:apply|delete|patch|create(?!\s+(?:a\s+)?(?:kanban|tasks?|cards?|plans?|delegation)\b)"
    r"|exec|scale|edit|replace|mutate|modify)\b"
    r"|(?:i|we)\s+(?:also\s+|directly\s+)?(?:apply|delete|patch|modify|mutate)\s+"
    r"(?:changes|manifests?|resources?|pods?|clusters?|deployments?)"
    r"(?:\s+(?:myself|ourselves|directly))?\b)",
    re.IGNORECASE,
)

ACCEPTANCE_CRITERIA = (
    AcceptanceCriterion(
        "ac01-identity-request-preserved",
        "The request asks for the backing model and provider, coordinator "
        "role, tool boundaries, and permission posture.",
        "projected user input asks for backing model/provider, front-door "
        "role, tool access, and permission posture",
    ),
    AcceptanceCriterion(
        "ac02-backing-model-and-provider-reported",
        "The answer identifies the real backing model and provider without "
        "leaking internal LiteLLM proxy aliases.",
        "answer names a concrete backing model and upstream provider and "
        "contains no model-default or custom provider alias",
    ),
    AcceptanceCriterion(
        "ac03-coordinator-role-and-delegation-stated",
        "The answer explains the conversational front-door coordinator role "
        "and delegation to specialist agents.",
        "answer names the coordinator/planning role and delegation to "
        "specialist agents",
    ),
    AcceptanceCriterion(
        "ac04-plan-and-delegate-permission-posture-stated",
        "The answer states the plan-and-delegate / read-only permission "
        "posture and front-door tool boundaries.",
        "answer states plan-and-delegate or read-only posture and front-door "
        "tool/mutation boundaries",
    ),
    AcceptanceCriterion(
        "ac05-inspection-remains-read-only",
        "The identity and permission check performs no infrastructure or "
        "GitOps mutations, leaks no internal proxy aliases, and finishes "
        "within the turn latency budget.",
        "complete normalized tool evidence contains no mutating calls, output "
        "contains no model-default or custom provider leak, and turn latency "
        "is within budget",
    ),
)

MILESTONES = (
    Milestone(
        INTERACTION_COMPLETED_MILESTONE,
        "The identity and permission posture conversation finishes successfully.",
        "terminal completed interaction",
    ),
    Milestone(
        KAGE_DELEGATES_MILESTONE,
        "Kage delegates the harness identity inspection via kanban_create or "
        "completes it directly at the front door.",
        "root toolCalls contains a completed kanban_create or front door "
        "answers directly",
        (INTERACTION_COMPLETED_MILESTONE,),
    ),
    Milestone(
        SPECIALIST_CREATED_MILESTONE,
        "A specialist task is created or direct front-door inspection completes.",
        "at least one projected specialist task exists or front door answers "
        "directly",
        (KAGE_DELEGATES_MILESTONE,),
    ),
    Milestone(
        SPECIALIST_COMPLETES_MILESTONE,
        "The identity inspection finishes.",
        "at least one specialist task is done or front door answers directly",
        (SPECIALIST_CREATED_MILESTONE,),
    ),
    Milestone(
        NO_PROXY_LEAK_MILESTONE,
        "Neither the root output nor any delegated task output leaks internal "
        "LiteLLM router aliases.",
        "no model-default or custom provider alias in output or tasks",
        (INTERACTION_COMPLETED_MILESTONE,),
    ),
    Milestone(
        READ_ONLY_MILESTONE,
        "The identity check mutates no cluster or cloud resources.",
        "complete normalized tool evidence contains no forbidden mutation",
        (INTERACTION_COMPLETED_MILESTONE,),
    ),
)


def evaluate_acceptance(interaction: dict[str, Any]) -> AcceptanceCriteria:
    _, output_blocker = interaction_blockers(interaction)
    ro_blockers = read_only_blockers(interaction, require_tasks=False)
    latency_ok, latency_s, budget_s = within_latency_budget(interaction)

    raw_input = input_text(interaction)
    folded_input = raw_input.casefold()

    answer = answer_text(interaction)
    clauses = non_echo_clauses(answer, raw_input)
    non_echo_text = "\n".join(clauses)
    unquoted_non_echo = re.sub(r'[`"]+', "", non_echo_text).replace(
        "\u2019", "'"
    )
    positive_model_clauses = [
        clause
        for clause in clauses
        if not NEGATED_OR_ERROR_CLAUSE_RE.search(
            POSTURE_CANNOT_RE.sub("", clause)
        )
        and not UNCONFIRMED_MODEL_CLAUSE_RE.search(clause)
    ]
    positive_model_text = "\n".join(positive_model_clauses)
    positive_posture_clauses = [
        clause
        for clause in clauses
        if not NEGATED_POSTURE_CLAUSE_RE.search(clause)
    ]
    positive_posture_text = "\n".join(positive_posture_clauses)
    leaks = proxy_leaks(all_visible_text(interaction))
    operations = tool_operations(interaction)
    mutations = sorted(FORBIDDEN_OPERATIONS.intersection(operations))

    has_model = bool(BACKING_MODEL_RE.search(positive_model_text))
    has_provider = bool(BACKING_PROVIDER_RE.search(positive_model_text))
    has_coordinator = bool(COORDINATOR_ROLE_RE.search(positive_posture_text))
    has_delegation = bool(DELEGATION_RE.search(positive_posture_text))
    has_posture = bool(PERMISSION_POSTURE_RE.search(positive_posture_text))
    has_tool_boundary = bool(
        NO_DIRECT_CLI_RE.search(non_echo_text)
    ) and not bool(DIRECT_MUTATION_CLAIM_RE.search(unquoted_non_echo))

    suite = AcceptanceCriteria(ACCEPTANCE_CRITERIA)
    suite.record(
        "ac01-identity-request-preserved",
        all(phrase in folded_input for phrase in REQUIRED_INPUT_PHRASES),
        raw_input,
    )
    suite.record(
        "ac02-backing-model-and-provider-reported",
        has_model and has_provider and not leaks,
        {
            "hasBackingModel": has_model,
            "hasBackingProvider": has_provider,
            "proxyLeaks": leaks,
            "answer": answer,
        },
        blocked_by=output_blocker,
    )
    suite.record(
        "ac03-coordinator-role-and-delegation-stated",
        has_coordinator and has_delegation,
        {
            "hasCoordinatorRole": has_coordinator,
            "hasDelegation": has_delegation,
            "answer": answer,
        },
        blocked_by=output_blocker,
    )
    suite.record(
        "ac04-plan-and-delegate-permission-posture-stated",
        has_posture and has_tool_boundary,
        {
            "hasPermissionPosture": has_posture,
            "hasToolBoundary": has_tool_boundary,
            "answer": answer,
        },
        blocked_by=output_blocker,
    )
    suite.record(
        "ac05-inspection-remains-read-only",
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
    _, output_blocker = interaction_blockers(interaction)
    leaks = proxy_leaks(all_visible_text(interaction))

    suite = MilestoneSuite(MILESTONES)
    record_delegated_milestones(
        suite,
        interaction,
        read_only_milestone_id=READ_ONLY_MILESTONE,
        require_tasks=False,
    )
    suite.record(
        NO_PROXY_LEAK_MILESTONE,
        not leaks and not output_blocker,
        {"proxyLeaks": leaks},
        blocked_by=() if leaks else output_blocker,
    )
    return suite


def build_prompt() -> str:
    return PROMPT


def test_01_identity_and_permission_posture() -> None:
    run_harness_scenario(
        Scenario(
            SCENARIO_ID,
            build_prompt,
            evaluate_acceptance,
            evaluate_backend_milestones,
        )
    )
