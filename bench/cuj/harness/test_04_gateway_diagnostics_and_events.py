"""Reproduce harness CUJ4: read-only diagnostics for CrashLoopBackOff, Warning events, and gateway logs."""

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

SCENARIO_ID = "cuj_harness_04"
GATEWAY_WORKLOAD_NAME = "platform-agent-gateway"
READ_ONLY_MILESTONE = "m4-diagnostics-remains-read-only"
REQUIRED_INPUT_PHRASES = (
    "crashloopbackoff",
    "restart counts",
    "warning events",
    GATEWAY_WORKLOAD_NAME,
)

PROMPT = """Run a read-only diagnostic check on this host cluster's \
kube-agents harness: (1) check whether any pods are in CrashLoopBackOff or \
have non-zero restart counts, (2) check for any Warning events across the \
cluster from the last 15 minutes, and (3) inspect the status and the last 5 \
log lines of the platform-agent-gateway pod in kubeagents-system (or state \
the exact RBAC boundary if log access is restricted). Include the \
CrashLoopBackOff/restart findings, Warning event findings, and \
platform-agent-gateway status/log findings in both the task completion \
summary and the full result. Do not restart pods or mutate any cluster \
resources."""

SKIPPED_CHECK_RE = re.compile(
    r"\b(?:not\s+(?:checked|inspected|retrieved|fetched|read|collected|available|attempted)"
    r"|unchecked|skipped|omitted"
    r"|(?:unable|failed)\s+to\s+(?:check|inspect|retrieve|fetch|read|get|access|collect)"
    r"|(?:could|can|did|were|was)\s+not\s+(?:check|inspect|retrieve|fetch|read|get|access|collect|inspected|checked|retrieved)"
    r"|cannot\s+(?:check|inspect|retrieve|fetch|read|get|access|collect))\b",
    re.IGNORECASE,
)
CRASHLOOP_RE = re.compile(
    r"\bcrash[-_ ]?loop(?:ing|s|ed|[-_ ]?back[-_ ]?off)?\b",
    re.IGNORECASE,
)
RESTART_RE = re.compile(r"\brestarts?\b", re.IGNORECASE)
WARNING_RE = re.compile(r"\bwarnings?\b", re.IGNORECASE)
EVENT_RE = re.compile(r"\bevents?\b", re.IGNORECASE)
PROCEDURAL_PREFIX_RE = re.compile(
    r"^\s*(?:(?:i|we)\s+(?:have\s+|had\s+)?)?"
    r"(?:check(?:ed|ing)?|review(?:ed|ing)?|inspect(?:ed|ing)?|audit(?:ed|ing)?"
    r"|look(?:ed|ing)?\s+for|quer(?:ied|ying)|examin(?:ed|ing))\s+"
    r"(?:whether|if|for|the|any|all|recent|cluster"
    r"|crash[-_ ]?loop(?:[-_ ]?back[-_ ]?off)?|restarts?|warnings?)\b"
    r"(?:(?!\b(?:found|detected|observed|recorded|confirmed|show(?:s|ed)?|none|zero|(?<!-)0)\b)[^:])*",
    re.IGNORECASE,
)
_COUNT_TOKEN = (
    r"(?<!non-)(?<!-)\b(?:\d+|zero|no|none|one|two|three|four|five|six|seven"
    r"|eight|nine|ten|several|multiple)\b"
)
_NON_TIME_MODIFIER = (
    r"(?!(?:minutes?|mins?|seconds?|secs?|hours?|hrs?|days?|for|across|in)\b)"
    r"[a-z0-9_-]+"
)
DIAGNOSTIC_OUTCOME_RE = re.compile(
    rf"{_COUNT_TOKEN}\s+(?:{_NON_TIME_MODIFIER}\s+){{0,3}}?"
    rf"(?:restarts?|warnings?|events?"
    rf"|crash[-_ ]?loop(?:ing|s|[-_ ]?back[-_ ]?off)?)\b"
    rf"|(?<!non-)(?<!-)\b(?:0|zero|no|none)\s+(?:{_NON_TIME_MODIFIER}\s+){{0,3}}?"
    rf"(?:pods?|containers?|workloads?)\b"
    rf"|\b(?:crash[-_ ]?loop(?:ing|s|[-_ ]?back[-_ ]?off)?|restarts?(?:\s+counts?)?"
    rf"|warning\s+events?|events?)\b"
    rf"[^.;\n]{{0,40}}?(?:[:=]\s*(?:{_COUNT_TOKEN}|\b(?:clean|healthy)\b)"
    rf"|\b(?:is|are|was|were|at|with|of|have|has)\s+(?:{_COUNT_TOKEN}|\b(?:clean|healthy)\b)"
    rf"|(?<!non-)(?<!-)\b(?:none|zero|no\s+\w+)\s+(?:were\s+|was\s+)?(?:found|detected|observed|present|recorded|seen)\b)"
    rf"|(?<!non-)(?<!-)\b(?:no|none|zero|without(?:\s+any)?|neither)\b[^.;\n]{{0,40}}?"
    rf"\b(?:crash[-_ ]?loop(?:ing|s|[-_ ]?back[-_ ]?off)?|restarts?|warning\s+events?)\b"
    rf"|\b(?:found|detected|observed|recorded)\s+(?:{_COUNT_TOKEN}|(?:\s*{_NON_TIME_MODIFIER}){{0,3}}\s+"
    rf"(?:crash[-_ ]?loop(?:ing|s|[-_ ]?back[-_ ]?off)?|restarts?|warning\s+events?)\b)"
    rf"|(?<!\bwhether\s)(?<!\bif\s)(?<!\bany\s)(?:"
    rf"{_COUNT_TOKEN}\s+pods?|\b(?:(?:the\s+)?[a-z0-9-]+\s+pods?|pods?\s+[a-z0-9-]+|[a-z0-9]+-[a-z0-9-]+)"
    rf")\s+(?:is|are|was|were)\s+(?:in\s+)?crash[-_ ]?loop(?:ing|s|[-_ ]?back[-_ ]?off)?\b"
    rf"|\ball\s+(?:\d+\s+)?(?:{_NON_TIME_MODIFIER}\s+){{0,2}}?(?:pods?|containers?|workloads?)\s+(?:are\s+)?(?:healthy|running|ready|clean)\b",
    re.IGNORECASE,
)
GATEWAY_RE = re.compile(r"\bplatform-?agent-gateway\b", re.IGNORECASE)
GATEWAY_ANAPHORA_RE = re.compile(
    r"\b(?:its|the\s+gateway(?:'s)?|gateway)\s+(?:recent\s+|last\s+\d+\s+)?logs?\b",
    re.IGNORECASE,
)
GATEWAY_STATUS_RE = re.compile(
    r"\b(?:running|ready|healthy|crashloopbackoff|backoff|error|failed|pending"
    r"|unhealthy|\d+/\d+)\b",
    re.IGNORECASE,
)
GATEWAY_RBAC_RE = re.compile(
    r"\b(?:forbidden|permission\s+denied|pods/log"
    r"|rbac\s+(?:forbids|denies|prevent\w*|restrict\w*|boundary|block\w*)"
    r"|(?:restricted|blocked|denied|forbidden)\s+by\s+rbac)\b",
    re.IGNORECASE,
)
GATEWAY_LOG_EVIDENCE_RE = re.compile(
    r"\b(?:healthz|200\s+ok|connection\s+refused|listening\s+on"
    r"|logs?\s+(?:show|shows|contain|contains|report|reports|indicate|indicates|confirm|confirms|include|includes|state|states)"
    r"|last\s+\d+\s+log\s+lines?\s*[:=-]"
    r"|recent\s+logs?\s*[:=-]"
    r"|no\s+errors?\s+in\s+(?:the\s+)?(?:recent\s+)?logs?)\b",
    re.IGNORECASE,
)
GATEWAY_LOG_OR_RBAC_RE = re.compile(
    f"(?:{GATEWAY_LOG_EVIDENCE_RE.pattern}|{GATEWAY_RBAC_RE.pattern})",
    re.IGNORECASE,
)

ACCEPTANCE_CRITERIA = (
    AcceptanceCriterion(
        "ac01-diagnostics-request-preserved",
        "The request asks for CrashLoopBackOff/restart checks, recent Warning "
        "events, and platform-agent-gateway status and logs.",
        "projected user input names CrashLoopBackOff, restart counts, Warning "
        "events, and platform-agent-gateway",
    ),
    AcceptanceCriterion(
        "ac02-crashloop-and-restarts-checked",
        "The answer reports the CrashLoopBackOff and container restart audit "
        "outcome.",
        "delivered answer addresses both CrashLoopBackOff state and container "
        "restart counts with a positive or negative diagnostic outcome",
    ),
    AcceptanceCriterion(
        "ac03-warning-events-checked",
        "The answer reports the cluster Warning events check for the recent "
        "window.",
        "delivered answer addresses Warning events across the cluster with a "
        "positive or negative diagnostic outcome",
    ),
    AcceptanceCriterion(
        "ac04-gateway-status-and-logs-reported",
        "The answer reports platform-agent-gateway status and recent log "
        "lines or an explicit RBAC boundary.",
        "delivered answer names platform-agent-gateway alongside pod/container "
        "status and recent log findings or an explicit RBAC restriction",
    ),
    AcceptanceCriterion(
        "ac05-diagnostics-remains-read-only",
        "The diagnostic check performs no mutations, leaks no internal "
        "LiteLLM proxy aliases, and finishes within the turn latency budget.",
        "complete normalized tool evidence contains no mutating calls, output "
        "contains no model-default or custom provider leak, and turn latency "
        "is within budget",
    ),
)

MILESTONES = (
    Milestone(
        INTERACTION_COMPLETED_MILESTONE,
        "The gateway diagnostics and events conversation finishes "
        "successfully.",
        "terminal completed interaction",
    ),
    Milestone(
        KAGE_DELEGATES_MILESTONE,
        "Kage delegates the cluster diagnostic check via kanban_create.",
        "root toolCalls contains a completed kanban_create",
        (INTERACTION_COMPLETED_MILESTONE,),
    ),
    Milestone(
        SPECIALIST_CREATED_MILESTONE,
        "A specialist task is created to run the read-only cluster diagnostics.",
        "at least one projected specialist task exists",
        (KAGE_DELEGATES_MILESTONE,),
    ),
    Milestone(
        SPECIALIST_COMPLETES_MILESTONE,
        "The delegated specialist diagnostic task finishes.",
        "at least one specialist task is done after at least one run",
        (SPECIALIST_CREATED_MILESTONE,),
    ),
    Milestone(
        READ_ONLY_MILESTONE,
        "Running the gateway and cluster diagnostics mutates nothing.",
        "complete normalized tool evidence contains no forbidden mutation",
        (INTERACTION_COMPLETED_MILESTONE,),
    ),
)


def _clause_has_outcome(
    clauses: list[str], *patterns: re.Pattern[str]
) -> bool:
    for clause in clauses:
        if SKIPPED_CHECK_RE.search(clause) and not GATEWAY_RBAC_RE.search(
            clause
        ):
            continue
        proc_match = PROCEDURAL_PREFIX_RE.search(clause)
        candidate = (
            clause[proc_match.end() :].lstrip(": ")
            if proc_match is not None
            else clause
        )
        if not candidate:
            continue
        if all(pattern.search(clause) for pattern in patterns) and bool(
            DIAGNOSTIC_OUTCOME_RE.search(candidate)
        ):
            return True
    return False


def _gateway_status_and_logs_reported(clauses: list[str]) -> bool:
    for idx, clause in enumerate(clauses):
        is_gateway_related = bool(
            GATEWAY_RE.search(clause)
            or (
                GATEWAY_ANAPHORA_RE.search(clause)
                and any(GATEWAY_RE.search(prev) for prev in clauses[:idx])
            )
        )
        if (
            is_gateway_related
            and SKIPPED_CHECK_RE.search(clause)
            and not GATEWAY_RBAC_RE.search(clause)
        ):
            return False

    gateway_clauses: list[str] = []
    gateway_log_candidates: list[str] = []
    for idx, clause in enumerate(clauses):
        if SKIPPED_CHECK_RE.search(clause) and not GATEWAY_RBAC_RE.search(
            clause
        ):
            continue
        if GATEWAY_RE.search(clause):
            gateway_clauses.append(clause)
            gateway_log_candidates.append(clause)
        elif GATEWAY_ANAPHORA_RE.search(clause) and any(
            GATEWAY_RE.search(prev) for prev in clauses[:idx]
        ):
            gateway_log_candidates.append(clause)

    if not gateway_clauses:
        return False
    has_status = any(GATEWAY_STATUS_RE.search(c) for c in gateway_clauses)
    has_log_or_rbac = any(
        GATEWAY_LOG_OR_RBAC_RE.search(c) for c in gateway_log_candidates
    )
    return has_status and has_log_or_rbac


def evaluate_acceptance(interaction: dict[str, Any]) -> AcceptanceCriteria:
    _, output_blocker = interaction_blockers(interaction)
    ro_blockers = read_only_blockers(interaction)
    latency_ok, latency_s, budget_s = within_latency_budget(interaction)

    raw_input = input_text(interaction)
    folded_input = raw_input.casefold()

    answer = answer_text(interaction)
    clauses = non_echo_clauses(answer, raw_input)
    non_rbac_clauses = [
        clause for clause in clauses if not GATEWAY_RBAC_RE.search(clause)
    ]
    leaks = proxy_leaks(all_visible_text(interaction))
    operations = tool_operations(interaction)
    mutations = sorted(FORBIDDEN_OPERATIONS.intersection(operations))
    refusal_or_clarification = has_clarification_or_refusal(
        "\n".join(non_rbac_clauses)
    )

    crashloop_checked = (
        _clause_has_outcome(clauses, CRASHLOOP_RE)
        and _clause_has_outcome(clauses, RESTART_RE)
        and not refusal_or_clarification
    )
    warnings_checked = (
        _clause_has_outcome(clauses, WARNING_RE, EVENT_RE)
        and not refusal_or_clarification
    )
    gateway_checked = (
        _gateway_status_and_logs_reported(clauses)
        and not refusal_or_clarification
    )

    suite = AcceptanceCriteria(ACCEPTANCE_CRITERIA)
    suite.record(
        "ac01-diagnostics-request-preserved",
        all(phrase in folded_input for phrase in REQUIRED_INPUT_PHRASES),
        raw_input,
    )
    suite.record(
        "ac02-crashloop-and-restarts-checked",
        crashloop_checked,
        {
            "crashloopAndRestartsChecked": crashloop_checked,
            "clarificationOrRefusal": refusal_or_clarification,
            "answer": answer,
        },
        blocked_by=output_blocker,
    )
    suite.record(
        "ac03-warning-events-checked",
        warnings_checked,
        {
            "warningEventsChecked": warnings_checked,
            "clarificationOrRefusal": refusal_or_clarification,
            "answer": answer,
        },
        blocked_by=output_blocker,
    )
    suite.record(
        "ac04-gateway-status-and-logs-reported",
        gateway_checked,
        {
            "gatewayChecked": gateway_checked,
            "clarificationOrRefusal": refusal_or_clarification,
            "answer": answer,
        },
        blocked_by=output_blocker,
    )
    suite.record(
        "ac05-diagnostics-remains-read-only",
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


def test_04_gateway_diagnostics_and_events() -> None:
    run_harness_scenario(
        Scenario(
            SCENARIO_ID,
            build_prompt,
            evaluate_acceptance,
            evaluate_backend_milestones,
        )
    )
