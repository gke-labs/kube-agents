"""Shared read-only evaluation and WebUI execution helpers for harness CUJs."""

from __future__ import annotations

import ipaddress
import math
import os
import re
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import ExitStack, contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any, Iterator

import pytest

from cuj.utils.evidence import EvidenceLog
from cuj.utils.interaction import (
    delivered_answer,
    projected_tasks,
    tool_operations,
    unnormalized_tool_calls,
)
from cuj.utils.milestones import MilestoneSuite
from cuj.utils.portal import REPO_ROOT, Portal, PortalError

if TYPE_CHECKING:
    from cuj.utils.scenario import Scenario

COMPLETED_STATUS = "completed"
RUNNING_STATUS = "running"
DONE_TASK_STATUS = "done"
ASSISTANT_ROLE = "assistant"
KANBAN_AUTHOR = "kanban"
DEFAULT_ASSIGNEE = "platform"
SILENT_REPLY_SENTINEL = "[SILENT]"
KANBAN_CREATE_OPERATION = "kanban_create"
INTERACTION_COMPLETED_MILESTONE = "interaction-completed"
KAGE_DELEGATES_MILESTONE = "m1-kage-delegates"
SPECIALIST_CREATED_MILESTONE = "m2-specialist-task-created"
SPECIALIST_COMPLETES_MILESTONE = "m3-specialist-completes"

WEB_CONSOLE_URL_ENV = "CUJ_WEB_CONSOLE_URL"
WEB_CONSOLE_CLUSTER_MODE_ENV = "CUJ_WEB_CONSOLE_CLUSTER_MODE"
WEB_CONSOLE_USE_PRIVATE_IP_ENV = "CUJ_WEB_CONSOLE_USE_PRIVATE_IP"
KEEP_PRIVATE_WEB_CONSOLE_ENV = "CUJ_KEEP_PRIVATE_WEB_CONSOLE"
TEARDOWN_WEB_CONSOLE_ENV = "CUJ_TEARDOWN_WEB_CONSOLE"
PROJECT_ID_ENV = "CUJ_PROJECT_ID"
CLUSTER_NAME_ENV = "CUJ_CLUSTER_NAME"
CLUSTER_LOCATION_ENV = "CUJ_CLUSTER_LOCATION"
MODEL_PROVIDER_ENV = "CUJ_MODEL_PROVIDER"
KUBE_CONTEXT_ENV = "CUJ_KUBE_CONTEXT"
KUBE_NAMESPACE_ENV = "CUJ_NAMESPACE"
TURN_TIMEOUT_ENV = "CUJ_TIMEOUT"
POLL_INTERVAL_ENV = "CUJ_POLL_INTERVAL"
DEFAULT_KUBE_NAMESPACE = "kubeagents-system"
DEFAULT_CLUSTER_LOCATION = "us-central1"
DEFAULT_MODEL_PROVIDER = "vertex_ai"
WEB_CONSOLE_SERVICE_NAME = "kube-agents-web-console"
WEB_CONSOLE_SERVICE_PORT = 8080
GATEWAY_DEPLOYMENT_NAME = "platform-agent-gateway"
HELM_RELEASE_NAME = "kube-agents"
EPHEMERAL_CLUSTER_PREFIX = "cuj-web-console-"
WEB_CONSOLE_READY_TIMEOUT_SECONDS = 60.0
INSTALL_TIMEOUT_SECONDS = 2400.0
UNINSTALL_TIMEOUT_SECONDS = 1800.0
KUBECTL_TIMEOUT_SECONDS = 120.0
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
INTERNAL_DNS_SUFFIXES = (
    ".svc",
    ".svc.cluster.local",
    ".cluster.local",
    ".internal",
)
PRIVATE_IP_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)
VALID_CLUSTER_MODES = frozenset({"create", "existing", "port-forward"})
TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "y", "on"})
MAX_LATENCY_SECONDS_ENV = "CUJ_MAX_LATENCY_SECONDS"
DEFAULT_MAX_LATENCY_SECONDS = 180.0
DEFAULT_TURN_TIMEOUT_SECONDS = 300.0
TRANSCRIPT_TIMEOUT_SECONDS = 30.0
TRANSCRIPT_SETTLE_TIMEOUT_SECONDS = 60.0
TRANSCRIPT_SETTLE_GRACE_SECONDS = 30.0
TRANSCRIPT_POLL_INTERVAL_SECONDS = 2.0
MIN_ECHO_CLAUSE_CHARS = 12
CONSOLE_HEADER = "X-Kube-Agents-Console"
WEB_CONSOLE_SESSION_PREFIX = "web-console-"
WEB_CONSOLE_RAND_BYTES = 16

INTERACTION_INCOMPLETE_BLOCKER = "interaction did not complete successfully"
OUTPUT_OMITTED_BLOCKER = "portal interaction projection omits output"
TOOL_EVIDENCE_OMITTED_BLOCKER = (
    "portal interaction projection omits toolEvidenceComplete"
)
WORKER_TOOLS_OMITTED_BLOCKER = "portal task projection omits worker toolCalls"
UNNORMALIZED_TOOLS_BLOCKER = "tool evidence omits normalized operations"
LATENCY_OMITTED_BLOCKER = "interaction projection omits turn latency"
LATENCY_EXCEEDED_BLOCKER = "turn latency exceeded configured budget"
VISIBLE_TASK_FIELDS = ("title", "result", "summary", "error")
CLARIFICATION_OR_REFUSAL_PHRASES = (
    "which cluster did you mean",
    "could you clarify",
    "confirm which cluster",
    "please specify which cluster",
    "cannot check",
    "unable to check",
)

FORBIDDEN_OPERATIONS = frozenset(
    {
        "apply_manifest",
        "create_cluster",
        "delete_cluster",
        "delete_pod",
        "delete_resource",
        "open_pull_request",
        "patch_resource",
        "rollout_restart",
        "scale_deployment",
        "submit_provisioning_request",
        "submit_suggestion",
    }
)
KNOWN_ROOT_OPERATIONS = frozenset(
    {
        "kanban_create",
        "kanban_get",
        "kanban_list",
        "kanban_update",
        "list_agents",
    }
)

PROXY_LEAK_RE = re.compile(
    r"\b(?:model[-_]default|custom\s+provider|provider\s*[:=]\s*['\"]?custom)\b",
    re.IGNORECASE,
)
CLAUSE_SPLIT_RE = re.compile(
    r"(?:\.(?=\s|$)|[!?\n;]+|\b(?:while|whereas|but|however|although)\b)",
    re.IGNORECASE,
)
NEGATED_OR_ERROR_CLAUSE_RE = re.compile(
    r"\b(?:could\s+not|couldn't|cannot|can't|unable\s+to|failed\s+to"
    r"|did\s+not|didn't|was\s+not|weren't|were\s+not"
    r"|not\s+(?:checked|inspected|enumerated|retrieved|obtained|attempted"
    r"|in\s+scope|determined)"
    r"|omitted\s+for\s+brevity|out\s+of\s+scope|unknown\s+project"
    r"|status\s+is\s+unknown|(?:permission|access)\s+denied"
    r"|(?:call|request|command|query|list|get|read|access|check|inspection)\s+"
    r"(?:was\s+|were\s+|is\s+)?(?:denied|forbidden)"
    r"|(?:denied|forbidden)\s+by\s+(?:rbac|iam|api|server)"
    r"|no\s+(?:[a-z0-9-]+\s+){0,3}?clusters?\s+(?:are\s+|were\s+)?"
    r"(?:found|visible|in\s+scope|present|listed)"
    r"|(?:(?<!-)0|zero)\s+(?:[a-z0-9-]+\s+){0,3}?clusters?\b)\b",
    re.IGNORECASE,
)
NODE_RE = re.compile(r"\bnodes?\b", re.IGNORECASE)
NON_NODE_WORKLOAD_RE = re.compile(
    r"\b(?:pods?|containers?|workloads?|services?)\b",
    re.IGNORECASE,
)
ZERO_NOTREADY_RE = re.compile(
    r"\b(?:(?<!-)0|no|none|zero)\s+(?:[a-z0-9_-]+\s+){0,2}?not\s*ready\b",
    re.IGNORECASE,
)
PROCEDURAL_NODE_READY_LABEL_RE = re.compile(
    r"(?<!\bin\s)\b(?:nodes?\s+ready\s+(?:status|state|condition|check|summary)"
    r"|ready\s+(?:status|state|condition|check)\s+(?:of|for)\s+(?:the\s+|cluster\s+)?nodes?"
    r"|their\s+ready\s+(?:status|state|condition|check|summary))\b",
    re.IGNORECASE,
)
NEGATED_NODE_READY_RE = re.compile(
    r"\b(?:no|not|none|never|neither|(?<!-)0|zero)\b[^,.\n;]{0,60}\bnodes?\b"
    r"|\bnodes?\b(?:(?!\b(?:pods?|containers?|workloads?|services?)\b)[^.\n;]){0,60}"
    r"\b(?:no|not|none|never|neither|isn't|aren't|wasn't|weren't"
    r"|(?<!-)0(?!\s*(?:/|[1-9]))|zero)\b[^.\n;]{0,30}\bready\b"
    r"|\b0\s*/\s*\d+\s*(?:nodes?\s+)?ready\b"
    r"|\bready\s*[:=]\s*(?:false|0|no|unknown)\b"
    r"|\bnot\s*ready\b",
    re.IGNORECASE,
)
POSITIVE_NODE_READY_RE = re.compile(
    r"\bnodes?\b(?:(?!\b(?:pods?|containers?|workloads?|services?)\b)[^.\n;]){0,60}"
    r"\bready(?!\s+(?:status|state|condition|check|summary)\b)\b"
    r"|\bnodes?\b(?:(?!\b(?:pods?|containers?|workloads?|services?)\b)[^.\n;]){0,60}"
    r"\bin\s+(?:a\s+|the\s+)?ready\s+(?:state|condition|status)\b"
    r"|\bready(?!\s+(?:status|state|condition|check|summary)\b)\b"
    r"(?:(?!\b(?:pods?|containers?|workloads?|services?)\b)[^.\n;]){0,40}\bnodes?\b",
    re.IGNORECASE,
)
_NODE_COUNT_WORD = (
    r"(?:(?<!-)[1-9]\d*|one|two|three|four|five|six|seven|eight|nine|ten)"
)
POSITIVE_NODE_COUNT_RE = re.compile(
    rf"\b(?:{_NODE_COUNT_WORD}\s+(?:[a-z0-9-]+\s+){{0,2}}?nodes?\b"
    rf"|nodes?(?:\s+count)?\s*(?:[:=]|is|are)?\s*{_NODE_COUNT_WORD}\b"
    rf"|[1-9]\d*\s*/\s*[1-9]\d*\s+(?:nodes?\s+)?ready\b)",
    re.IGNORECASE,
)
POSITIVE_CONTRAST_SEGMENT_RE = re.compile(
    r"(?::|=|\b(?:only|[1-9]\d*|one|two|three|four|five|six|seven|eight|nine|ten"
    r"|all|both|each|every|active|running|ready|bound|installed|present|enforcing"
    r"|enabled|healthy|available|configured|deployed|registered|established"
    r"|serving|listening|exposing|exposes?|allows?|permits?|restricts?|selects?"
    r"|targets?|routes?|forwards?|maps?|mounts?|uses?|provides?|handles?|operates?"
    r"|remains?|has|have|is|are|was|were|exist|exists)\b)",
    re.IGNORECASE,
)
_NON_INVENTORY_MODIFIER = (
    r"(?!(?:issues?|errors?|problems?|warnings?|pressure|taints?"
    r"|evictions?|restarts?|events?|instances?|changes?|mutations?)\b)"
    r"[a-z0-9_.*-]+"
)
KANBAN_RESULT_RE = re.compile(
    r"^Result:\s*(.+?)(?:\n\nThis is an automatic task-status notification|\Z)",
    re.MULTILINE | re.DOTALL,
)


def input_text(interaction: dict[str, Any]) -> str:
    """Return the projected user prompt text from ``interaction``."""

    user_input = interaction.get("input")
    return str(
        (user_input or {}).get("text") if isinstance(user_input, dict) else ""
    )


def answer_text(interaction: dict[str, Any]) -> str:
    """Return user-facing answer text from root output, task results, and summaries."""

    base = delivered_answer(interaction)
    parts = [base] if base else []
    for task in projected_tasks(interaction):
        summary = task.get("summary")
        if (
            isinstance(summary, str)
            and summary.strip()
            and summary.strip() not in base
            and summary.strip() not in parts
        ):
            parts.append(summary.strip())
    return "\n\n".join(parts)


def all_visible_text(interaction: dict[str, Any]) -> str:
    """Return every user-visible string across root output, errors, and tasks."""

    parts = [
        str(interaction.get("output") or ""),
        str(interaction.get("error") or ""),
        answer_text(interaction),
    ]
    for task in projected_tasks(interaction):
        for field in VISIBLE_TASK_FIELDS:
            value = task.get(field)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
    return "\n".join(part for part in parts if part)


def non_echo_clauses(answer: str, prompt: str) -> list[str]:
    """Return non-empty clauses from ``answer`` that do not verbatim echo ``prompt``."""

    folded_prompt = prompt.casefold()
    clauses: list[str] = []
    for raw_clause in CLAUSE_SPLIT_RE.split(answer):
        clause = raw_clause.strip()
        if not clause:
            continue
        if (
            len(clause) >= MIN_ECHO_CLAUSE_CHARS
            and clause.casefold() in folded_prompt
        ):
            continue
        clauses.append(clause)
    return clauses


def negated_inventory_clause_re(*nouns: str) -> re.Pattern[str]:
    """Build a clause-level regex matching negated or absent inventory for ``nouns``."""

    joined = "|".join(nouns)
    return re.compile(
        rf"\b(?:no|none|nothing|neither|never"
        rf"|(?<!-)0(?!\s*(?:restarts?|errors?|warnings?|issues?|failures?|not\s*ready)\b)"
        rf"|zero|without|missing|absent|empty|lacks?)\b"
        rf"\s+(?:{_NON_INVENTORY_MODIFIER}\s+){{0,4}}?(?:{joined})\b"
        rf"|\b(?:{joined})\b[^,:\n;]{{0,50}}\b(?:"
        rf"not\s+(?:running|found|installed|present|exposed|bound|in\s+use|inspected"
        rf"|listed|enumerated|active|applied|enforced|restricted|available)"
        rf"|do\s+not\s+(?:exist|apply)|doesn't\s+(?:exist|apply)|did\s+not\s+exist"
        rf"|is\s+(?:empty|missing|absent|unknown)"
        rf"|are\s+(?:empty|missing|absent|unknown)"
        rf"|were\s+not\s+(?:found|installed|present|inspected|listed|enumerated)"
        rf"|was\s+not\s+(?:found|installed|present|inspected|listed)"
        rf"|missing|absent)\b",
        re.IGNORECASE,
    )


def positive_inventory_clauses(
    answer: str,
    prompt: str,
    *nouns: str,
) -> list[str]:
    """Return non-echo clauses that are neither error/refusal nor negated for ``nouns``."""

    negated_re = negated_inventory_clause_re(*nouns) if nouns else None
    result: list[str] = []
    for clause in non_echo_clauses(answer, prompt):
        if NEGATED_OR_ERROR_CLAUSE_RE.search(clause):
            continue
        scrubbed_clause = ZERO_NOTREADY_RE.sub("", clause)
        if negated_re is not None and negated_re.search(scrubbed_clause):
            kept_segments = [
                seg.strip()
                for seg in clause.split(",")
                if seg.strip()
                and not negated_re.search(ZERO_NOTREADY_RE.sub("", seg))
                and POSITIVE_CONTRAST_SEGMENT_RE.search(seg)
            ]
            if not kept_segments:
                continue
            result.append(", ".join(kept_segments))
            continue
        result.append(clause)
    return result


def _candidate_has_positive_node_ready(
    text: str,
    *,
    require_count: bool,
) -> bool:
    scrubbed = PROCEDURAL_NODE_READY_LABEL_RE.sub(
        "", ZERO_NOTREADY_RE.sub("", text)
    )
    if not NODE_RE.search(scrubbed):
        return False
    if NEGATED_NODE_READY_RE.search(scrubbed):
        return False
    if not POSITIVE_NODE_READY_RE.search(scrubbed):
        return False
    if require_count and not POSITIVE_NODE_COUNT_RE.search(scrubbed):
        return False
    return True


def has_positive_node_ready(
    clauses: list[str],
    *,
    require_count: bool = False,
) -> bool:
    """Return whether ``clauses`` report positive cluster node readiness."""

    for clause in clauses:
        if NEGATED_OR_ERROR_CLAUSE_RE.search(clause):
            continue
        if _candidate_has_positive_node_ready(
            clause, require_count=require_count
        ):
            return True
    for idx in range(len(clauses) - 1):
        first, second = clauses[idx], clauses[idx + 1]
        if NEGATED_OR_ERROR_CLAUSE_RE.search(
            first
        ) or NEGATED_OR_ERROR_CLAUSE_RE.search(second):
            continue
        if NON_NODE_WORKLOAD_RE.search(first) or NON_NODE_WORKLOAD_RE.search(
            second
        ):
            continue
        if NEGATED_NODE_READY_RE.search(
            ZERO_NOTREADY_RE.sub("", first)
        ) or NEGATED_NODE_READY_RE.search(ZERO_NOTREADY_RE.sub("", second)):
            continue
        combined = f"{first} , {second}"
        if _candidate_has_positive_node_ready(
            combined, require_count=require_count
        ):
            return True
    return False


def proxy_leaks(text: str) -> list[str]:
    """Return sorted lowercase matches of internal LiteLLM proxy aliases in ``text``."""

    return sorted(
        {match.group(0).casefold() for match in PROXY_LEAK_RE.finditer(text)}
    )


def has_clarification_or_refusal(text: str) -> bool:
    """Whether ``text`` asks the user to clarify the target cluster or refuses the check."""

    folded = text.casefold()
    return any(phrase in folded for phrase in CLARIFICATION_OR_REFUSAL_PHRASES)


def max_latency_seconds() -> float:
    """Return the configured turn latency budget in seconds."""

    raw = os.environ.get(MAX_LATENCY_SECONDS_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_LATENCY_SECONDS
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{MAX_LATENCY_SECONDS_ENV} must be positive")
    return value


def interaction_latency_seconds(interaction: dict[str, Any]) -> float | None:
    """Return turn latency in seconds from ``latencySeconds`` or ``createdAt``/``updatedAt``."""

    raw = interaction.get("latencySeconds")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    created_raw = interaction.get("createdAt")
    updated_raw = interaction.get("updatedAt")
    if isinstance(created_raw, str) and isinstance(updated_raw, str):
        try:
            created = datetime.fromisoformat(created_raw)
            updated = datetime.fromisoformat(updated_raw)
        except ValueError:
            return None
        return (updated - created).total_seconds()
    return None


def within_latency_budget(
    interaction: dict[str, Any],
) -> tuple[bool, float | None, float]:
    """Return ``(within_budget, observed_latency_seconds, budget_seconds)``."""

    budget = float(
        interaction.get("maxLatencySeconds") or max_latency_seconds()
    )
    observed = interaction_latency_seconds(interaction)
    if observed is None:
        return False, None, budget
    return 0.0 <= observed <= budget, round(observed, 3), budget


def interaction_blockers(
    interaction: dict[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(interaction_blocker, output_blocker)`` tuples for acceptance evaluation."""

    interaction_blocker = (
        ()
        if interaction.get("status") == COMPLETED_STATUS
        and interaction.get("terminal") is True
        else (INTERACTION_INCOMPLETE_BLOCKER,)
    )
    output_projected = bool(answer_text(interaction).strip())
    output_blocker = tuple(
        dict.fromkeys(
            (
                *interaction_blocker,
                *(
                    ()
                    if output_projected
                    else (OUTPUT_OMITTED_BLOCKER,)
                ),
            )
        )
    )
    return interaction_blocker, output_blocker


def _read_only_evidence_blockers(
    interaction: dict[str, Any],
    *,
    require_tasks: bool = True,
) -> tuple[str, ...]:
    """Return tool-evidence and latency blockers shared by read-only acceptance and milestones."""

    tasks = projected_tasks(interaction)
    operations = tool_operations(interaction)
    unnormalized_calls = unnormalized_tool_calls(interaction)
    needs_worker_tasks = require_tasks or KANBAN_CREATE_OPERATION in operations
    worker_tools_available = (
        bool(tasks) or not needs_worker_tasks
    ) and all("toolCalls" in task for task in tasks)
    latency_ok, latency_s, _ = within_latency_budget(interaction)

    return tuple(
        reason
        for condition, reason in (
            (
                interaction.get("toolEvidenceComplete") is not True,
                TOOL_EVIDENCE_OMITTED_BLOCKER,
            ),
            (
                not worker_tools_available,
                WORKER_TOOLS_OMITTED_BLOCKER,
            ),
            (
                bool(unnormalized_calls),
                UNNORMALIZED_TOOLS_BLOCKER,
            ),
            (
                latency_s is None,
                LATENCY_OMITTED_BLOCKER,
            ),
            (
                latency_s is not None and not latency_ok,
                LATENCY_EXCEEDED_BLOCKER,
            ),
        )
        if condition
    )


def read_only_blockers(
    interaction: dict[str, Any],
    *,
    require_tasks: bool = True,
) -> tuple[str, ...]:
    """Return blockers for read-only and latency safety criteria when no mutation leaked."""

    _, output_blocker = interaction_blockers(interaction)
    return tuple(
        dict.fromkeys(
            (
                *output_blocker,
                *_read_only_evidence_blockers(
                    interaction, require_tasks=require_tasks
                ),
            )
        )
    )


def record_read_only_milestone(
    suite: MilestoneSuite,
    milestone_id: str,
    interaction: dict[str, Any],
    *,
    require_tasks: bool = True,
) -> None:
    """Record the read-only tool evidence milestone on ``suite``."""

    operations = tool_operations(interaction)
    unnormalized_calls = unnormalized_tool_calls(interaction)
    mutations = sorted(FORBIDDEN_OPERATIONS.intersection(operations))
    latency_ok, latency_s, budget_s = within_latency_budget(interaction)
    evidence_blockers = _read_only_evidence_blockers(
        interaction, require_tasks=require_tasks
    )

    suite.record(
        milestone_id,
        not evidence_blockers and not mutations and latency_ok,
        {
            "toolEvidenceComplete": interaction.get("toolEvidenceComplete"),
            "operations": operations,
            "mutations": mutations,
            "unnormalizedTools": unnormalized_calls,
            "latencySeconds": latency_s,
            "maxLatencySeconds": budget_s,
        },
        blocked_by=() if mutations else evidence_blockers,
    )


def record_delegated_milestones(
    suite: MilestoneSuite,
    interaction: dict[str, Any],
    *,
    read_only_milestone_id: str,
    require_tasks: bool = True,
) -> None:
    """Record standard delegation and read-only milestones on ``suite``."""

    tasks = projected_tasks(interaction)
    completed_operations = tool_operations(interaction, completed_only=True)
    delegated = KANBAN_CREATE_OPERATION in completed_operations
    direct_completed = (
        not require_tasks
        and not delegated
        and interaction.get("status") == COMPLETED_STATUS
        and interaction.get("terminal") is True
    )

    suite.record(
        INTERACTION_COMPLETED_MILESTONE,
        interaction.get("status") == COMPLETED_STATUS
        and interaction.get("terminal") is True,
        interaction.get("status"),
    )
    suite.record(
        KAGE_DELEGATES_MILESTONE,
        delegated or direct_completed,
        completed_operations,
    )
    suite.record(
        SPECIALIST_CREATED_MILESTONE,
        bool(tasks) or direct_completed,
        tasks,
    )
    suite.record(
        SPECIALIST_COMPLETES_MILESTONE,
        any(
            task.get("status") == DONE_TASK_STATUS
            and int(task.get("runCount") or 0) >= 1
            for task in tasks
        )
        or direct_completed,
        tasks,
    )
    record_read_only_milestone(
        suite,
        read_only_milestone_id,
        interaction,
        require_tasks=require_tasks,
    )


def _extract_worker_tool_calls(
    source: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """Extract normalized worker tool calls when ``source`` explicitly carries them."""

    raw = (
        source.get("toolCalls")
        if isinstance(source.get("toolCalls"), list)
        else source.get("tool_calls")
        if isinstance(source.get("tool_calls"), list)
        else None
    )
    if raw is None:
        return None
    calls: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("tool") or "").strip()
        operation = str(item.get("operation") or "").strip()
        status = str(item.get("status") or COMPLETED_STATUS).strip()
        call: dict[str, Any] = {"name": name, "status": status}
        if operation:
            call["operation"] = operation
        calls.append(call)
    return calls


def _project_web_console_interaction(
    prompt: str,
    session_id: str,
    chat_payload: dict[str, Any],
    transcript_payload: dict[str, Any],
    *,
    latency_seconds: float,
    budget_seconds: float,
) -> dict[str, Any]:
    """Project a Web Console ``/api/chat`` + ``/transcript`` turn into a CUJ interaction."""

    messages = [
        msg
        for msg in (transcript_payload.get("messages") or [])
        if isinstance(msg, dict)
    ]
    turn_settled = _transcript_turn_settled(transcript_payload)
    root_tool_calls: list[dict[str, Any]] = []
    delegated_cards: dict[str, dict[str, Any]] = {}

    assistant_replies: list[str] = []
    for msg in messages:
        if (
            msg.get("role") == ASSISTANT_ROLE
            and msg.get("author") != KANBAN_AUTHOR
        ):
            content = str(msg.get("content") or "").strip()
            if (
                content
                and content != SILENT_REPLY_SENTINEL
                and content not in assistant_replies
            ):
                assistant_replies.append(content)
            for step in msg.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                tool_name = str(step.get("tool") or step.get("name") or "").strip()
                step_operation = str(step.get("operation") or "").strip()
                if not step_operation and (
                    tool_name in KNOWN_ROOT_OPERATIONS
                    or tool_name in FORBIDDEN_OPERATIONS
                ):
                    step_operation = tool_name
                if tool_name or step_operation:
                    root_call: dict[str, Any] = {
                        "name": tool_name or step_operation,
                        "status": COMPLETED_STATUS,
                    }
                    if step_operation:
                        root_call["operation"] = step_operation
                    root_tool_calls.append(root_call)
                if step.get("delegation") and step.get("task_id"):
                    task_id = str(step["task_id"])
                    step_status = (
                        DONE_TASK_STATUS
                        if str(step.get("status") or "").strip().casefold()
                        in {"done", "completed"}
                        else "in_progress"
                    )
                    card_entry = delegated_cards.setdefault(
                        task_id,
                        {
                            "taskId": task_id,
                            "title": str(
                                step.get("task_title") or step.get("title") or ""
                            ),
                            "assignee": str(
                                step.get("assignee") or DEFAULT_ASSIGNEE
                            ),
                            "status": step_status,
                            "runCount": 1,
                        },
                    )
                    worker_calls = _extract_worker_tool_calls(step)
                    if worker_calls is not None:
                        card_entry["toolCalls"] = worker_calls
        elif msg.get("author") == KANBAN_AUTHOR:
            card = msg.get("card") if isinstance(msg.get("card"), dict) else {}
            task_id = str(
                card.get("task_id") or f"t_{msg.get('id', KANBAN_AUTHOR)}"
            )
            content = str(msg.get("content") or "")
            raw_card_status = str(card.get("status") or "").strip().casefold()
            if raw_card_status in {"completed", "done"} or (
                not raw_card_status and "completed" in content.casefold()
            ):
                card_status = DONE_TASK_STATUS
            elif raw_card_status:
                card_status = raw_card_status
            else:
                card_status = "in_progress"
            result_match = KANBAN_RESULT_RE.search(content)
            extracted_result = (
                result_match.group(1).strip() if result_match else ""
            )
            summary = str(card.get("summary") or extracted_result or "").strip()
            result_text = str(
                card.get("result") or extracted_result or summary
            ).strip()
            card_entry = delegated_cards.setdefault(
                task_id,
                {
                    "taskId": task_id,
                    "title": str(card.get("title") or ""),
                    "assignee": str(card.get("assignee") or DEFAULT_ASSIGNEE),
                    "status": card_status,
                    "runCount": 1,
                },
            )
            card_entry["status"] = card_status
            worker_calls = _extract_worker_tool_calls(card)
            if worker_calls is not None:
                card_entry["toolCalls"] = worker_calls
            if result_text:
                card_entry["result"] = result_text
            if summary:
                card_entry["summary"] = summary

    chat_reply = str(chat_payload.get("reply") or "").strip()
    if (
        chat_reply
        and chat_reply != SILENT_REPLY_SENTINEL
        and chat_reply not in assistant_replies
    ):
        assistant_replies.append(chat_reply)
    reply_text = "\n\n".join(assistant_replies)
    tasks_list = list(delegated_cards.values())
    explicit_evidence_complete = (
        transcript_payload.get("toolEvidenceComplete") is True
        or transcript_payload.get("tool_evidence_complete") is True
    )
    all_root_calls_normalized = all(
        bool(str(call.get("operation") or "").strip())
        for call in root_tool_calls
    )
    all_tasks_have_tool_calls = (
        bool(tasks_list)
        and all_root_calls_normalized
        and all(
            isinstance(task.get("toolCalls"), list)
            and bool(task["toolCalls"])
            and all(
                bool(str(call.get("operation") or "").strip())
                for call in task["toolCalls"]
                if isinstance(call, dict)
            )
            for task in tasks_list
        )
    )
    projected: dict[str, Any] = {
        "interactionId": session_id,
        "sessionId": session_id,
        "status": COMPLETED_STATUS if turn_settled else RUNNING_STATUS,
        "terminal": turn_settled,
        "input": {"text": prompt},
        "output": reply_text,
        "latencySeconds": round(latency_seconds, 3),
        "maxLatencySeconds": budget_seconds,
        "toolCalls": root_tool_calls,
        "tasks": tasks_list,
    }
    if turn_settled and (
        explicit_evidence_complete or all_tasks_have_tool_calls
    ):
        projected["toolEvidenceComplete"] = True
    return projected


def _transcript_turn_settled(transcript_payload: dict[str, Any]) -> bool:
    """Return whether all delegated cards and wake replies in ``transcript_payload`` settled."""

    messages = [
        msg
        for msg in (transcript_payload.get("messages") or [])
        if isinstance(msg, dict)
    ]
    if not messages:
        return False

    delegated_ids: list[str] = []
    for msg in messages:
        if msg.get("role") != ASSISTANT_ROLE:
            continue
        for step in msg.get("steps") or []:
            if (
                isinstance(step, dict)
                and step.get("delegation")
                and step.get("task_id")
            ):
                task_id = str(step["task_id"])
                if task_id not in delegated_ids:
                    delegated_ids.append(task_id)

    if not delegated_ids:
        return any(
            msg.get("role") == ASSISTANT_ROLE
            and msg.get("author") != KANBAN_AUTHOR
            for msg in messages
        )

    last_kanban_index = -1
    completed_ids: set[str] = set()
    for idx, msg in enumerate(messages):
        if msg.get("author") != KANBAN_AUTHOR:
            continue
        last_kanban_index = idx
        card = msg.get("card") if isinstance(msg.get("card"), dict) else {}
        content = str(msg.get("content") or "")
        raw_card_status = str(card.get("status") or "").strip().casefold()
        card_completed = raw_card_status in {"completed", "done"} or (
            not raw_card_status and "completed" in content.casefold()
        )
        if not card_completed:
            continue
        card_task_id = str(card.get("task_id") or "")
        if card_task_id:
            completed_ids.add(card_task_id)
        for task_id in delegated_ids:
            if task_id in content:
                completed_ids.add(task_id)

    if not all(task_id in completed_ids for task_id in delegated_ids):
        return False

    return any(
        msg.get("role") == ASSISTANT_ROLE
        and msg.get("author") != KANBAN_AUTHOR
        for msg in messages[last_kanban_index + 1 :]
    )


def _env_truthy(name: str, *, default: bool = False) -> bool:
    """Return whether environment variable ``name`` is set to a truthy value."""

    raw = os.environ.get(name, "").strip().casefold()
    if not raw:
        return default
    return raw in TRUTHY_ENV_VALUES


def _positive_env_float(name: str, default: float) -> float:
    """Return a positive float from environment variable ``name`` or ``default``."""

    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise PortalError(
            f"{name} must be a positive number, got {raw!r}"
        ) from exc
    if value <= 0 or not math.isfinite(value):
        raise PortalError(f"{name} must be greater than 0, got {raw!r}")
    return value


def validate_private_web_console_url(url: str) -> str:
    """Validate that ``url`` points to loopback, a private IP, or an internal DNS host."""

    cleaned = url.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PortalError(
            f"Web Console URL must be an absolute HTTP(S) URL: {url!r}"
        )
    hostname = parsed.hostname.strip().casefold()
    if hostname in LOOPBACK_HOSTS or hostname.endswith(INTERNAL_DNS_SUFFIXES):
        return cleaned
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError as exc:
        raise PortalError(
            f"Refusing non-private Web Console host {hostname!r} in {url!r}: "
            "the Web Console has no authentication and must only be accessed "
            "over loopback (127.0.0.1/localhost), an internal cluster DNS "
            "name, or a private RFC-1918 IP address."
        ) from exc
    if (
        not (
            ip.is_loopback
            or any(ip in network for network in PRIVATE_IP_NETWORKS)
        )
        or ip.is_unspecified
        or ip.is_multicast
    ):
        raise PortalError(
            f"Refusing non-private Web Console IP {hostname!r} in {url!r}: "
            "the Web Console has no authentication and must never be exposed "
            "on a public IP address."
        )
    return cleaned


def _web_console_transport(root: str, *, timeout: float) -> Portal:
    """Construct a ``Portal`` client with Web Console headers for ``root``."""

    validated = validate_private_web_console_url(root)
    transport = Portal(validated, timeout=timeout)
    transport.headers[CONSOLE_HEADER] = "1"
    hostname = (urllib.parse.urlsplit(validated).hostname or "").casefold()
    if hostname not in LOOPBACK_HOSTS:
        transport.headers["Host"] = f"127.0.0.1:{WEB_CONSOLE_SERVICE_PORT}"
    return transport


def _kubectl_cmd(
    *args: str,
    namespace: str | None = None,
    kube_context: str = "",
) -> list[str]:
    """Build a ``kubectl`` command scoped to ``kube_context`` and ``namespace``."""

    cmd = ["kubectl"]
    if kube_context:
        cmd.extend(["--context", kube_context])
    if namespace:
        cmd.extend(["-n", namespace])
    cmd.extend(args)
    return cmd


def _run_checked_command(
    cmd: list[str],
    *,
    timeout: float = KUBECTL_TIMEOUT_SECONDS,
    description: str,
) -> str:
    """Execute ``cmd`` in ``REPO_ROOT`` and wrap failures in ``PortalError``."""

    try:
        completed = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise PortalError(f"{description} failed: {detail}") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise PortalError(f"{description} failed: {exc}") from exc
    return completed.stdout.strip()


def _installer_supports_web_console() -> bool:
    """Return whether ``install.sh`` in ``REPO_ROOT`` supports ``--enable-web-console``."""

    install_script = REPO_ROOT / "install.sh"
    try:
        return "--enable-web-console" in install_script.read_text(
            encoding="utf-8"
        )
    except OSError:
        return False


def _chart_supports_web_console() -> bool:
    """Return whether ``charts/kube-agents`` schema declares ``webConsole``."""

    schema_path = REPO_ROOT / "charts" / "kube-agents" / "values.schema.json"
    try:
        return '"webConsole"' in schema_path.read_text(encoding="utf-8")
    except OSError:
        return False


def _cluster_already_exists(
    *,
    project_id: str,
    cluster_name: str,
    location: str,
) -> bool:
    """Return whether GKE cluster ``cluster_name`` already exists in ``project_id``."""

    try:
        probe = subprocess.run(
            [
                "gcloud",
                "container",
                "clusters",
                "describe",
                cluster_name,
                f"--location={location}",
                f"--project={project_id}",
                "--format=value(name)",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=KUBECTL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


def _provision_cluster_with_web_console(
    *,
    project_id: str,
    cluster_name: str,
    location: str,
    model_provider: str,
) -> None:
    """Stand up a GKE cluster with ``install.sh`` and the Web Console enabled.

    Requires the ``--enable-web-console`` flag introduced by sibling PR #2321.
    """

    if not _installer_supports_web_console():
        raise PortalError(
            "install.sh in this checkout does not support --enable-web-console "
            "(provided by sibling PR #2321); use a branch/cluster with the Web "
            "Console deployed and connect via CUJ_WEB_CONSOLE_URL or "
            "CUJ_WEB_CONSOLE_CLUSTER_MODE='port-forward'"
        )

    _run_checked_command(
        [
            str(REPO_ROOT / "install.sh"),
            "--non-interactive",
            f"--gcp-project-id={project_id}",
            f"--gcp-region={location}",
            f"--gke-cluster-name={cluster_name}",
            f"--model-provider={model_provider}",
            "--enable-web-console=true",
            "--allow-unverified-source",
        ],
        timeout=INSTALL_TIMEOUT_SECONDS,
        description=f"provisioning cluster {cluster_name!r} with web console",
    )


def _teardown_cluster(
    *,
    project_id: str,
    cluster_name: str,
    location: str,
) -> None:
    """Tear down a provisioned GKE cluster via ``uninstall.sh``."""

    _run_checked_command(
        [
            str(REPO_ROOT / "uninstall.sh"),
            "--non-interactive",
            f"--gcp-project-id={project_id}",
            f"--gcp-region={location}",
            f"--gke-cluster-name={cluster_name}",
        ],
        timeout=UNINSTALL_TIMEOUT_SECONDS,
        description=f"tearing down cluster {cluster_name!r}",
    )


def _deploy_web_console_on_existing_cluster(
    *,
    project_id: str,
    cluster_name: str,
    location: str,
    model_provider: str,
    namespace: str,
    kube_context: str,
) -> None:
    """Deploy the Web Console onto an existing cluster as a private Service.

    Requires the ``webConsole.enabled`` chart setting introduced by sibling PR #2321.
    """

    if cluster_name and project_id and not kube_context:
        _run_checked_command(
            [
                "gcloud",
                "container",
                "clusters",
                "get-credentials",
                cluster_name,
                f"--location={location}",
                f"--project={project_id}",
            ],
            description=(
                f"fetching credentials for existing cluster {cluster_name!r}"
            ),
        )

    probe = subprocess.run(
        _kubectl_cmd(
            "get",
            "deployment",
            GATEWAY_DEPLOYMENT_NAME,
            namespace=namespace,
            kube_context=kube_context,
        ),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=KUBECTL_TIMEOUT_SECONDS,
    )
    if probe.returncode == 0:
        if not _chart_supports_web_console():
            raise PortalError(
                "charts/kube-agents in this checkout does not declare "
                "webConsole.enabled (provided by sibling PR #2321); deploy the "
                "Web Console from the #2321 chart first and connect via "
                "CUJ_WEB_CONSOLE_URL or "
                "CUJ_WEB_CONSOLE_CLUSTER_MODE='port-forward'"
            )
        helm_cmd = [
            "helm",
            "upgrade",
            HELM_RELEASE_NAME,
            str(REPO_ROOT / "charts" / "kube-agents"),
            "-n",
            namespace,
            "--reuse-values",
            "--set",
            "webConsole.enabled=true",
            "--wait",
        ]
        if kube_context:
            helm_cmd.extend(["--kube-context", kube_context])
        _run_checked_command(
            helm_cmd,
            timeout=INSTALL_TIMEOUT_SECONDS,
            description="enabling web console via helm upgrade on existing cluster",
        )
        return

    if not project_id or not cluster_name:
        raise PortalError(
            "existing cluster does not have platform-agent-gateway deployed in "
            f"namespace {namespace!r}; set {PROJECT_ID_ENV} and "
            f"{CLUSTER_NAME_ENV} to install kube-agents with the Web Console"
        )
    _provision_cluster_with_web_console(
        project_id=project_id,
        cluster_name=cluster_name,
        location=location,
        model_provider=model_provider,
    )


def _teardown_web_console_on_existing_cluster(
    *,
    namespace: str,
    kube_context: str,
) -> None:
    """Disable and delete the unauthenticated Web Console on an existing cluster."""

    if _chart_supports_web_console():
        helm_cmd = [
            "helm",
            "upgrade",
            HELM_RELEASE_NAME,
            str(REPO_ROOT / "charts" / "kube-agents"),
            "-n",
            namespace,
            "--reuse-values",
            "--set",
            "webConsole.enabled=false",
            "--wait",
        ]
        if kube_context:
            helm_cmd.extend(["--kube-context", kube_context])
        subprocess.run(
            helm_cmd,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=KUBECTL_TIMEOUT_SECONDS,
        )
    _run_checked_command(
        _kubectl_cmd(
            "delete",
            "deployment,service,networkpolicy",
            WEB_CONSOLE_SERVICE_NAME,
            "--ignore-not-found=true",
            namespace=namespace,
            kube_context=kube_context,
        ),
        description="deleting web console resources on existing cluster",
    )


def _resolve_private_web_console_url(
    *,
    namespace: str,
    kube_context: str,
) -> str:
    """Resolve and validate the private IP endpoint of ``svc/kube-agents-web-console``."""

    raw = _run_checked_command(
        _kubectl_cmd(
            "get",
            "svc",
            WEB_CONSOLE_SERVICE_NAME,
            "-o",
            "jsonpath={.status.loadBalancer.ingress[0].ip},{.spec.clusterIP}",
            namespace=namespace,
            kube_context=kube_context,
        ),
        description=f"resolving private IP for service/{WEB_CONSOLE_SERVICE_NAME}",
    )
    parts = [
        part.strip()
        for part in raw.split(",")
        if part.strip() and part.strip().casefold() != "none"
    ]
    if not parts:
        raise PortalError(
            f"service/{WEB_CONSOLE_SERVICE_NAME} in namespace {namespace!r} "
            "has no private ClusterIP or internal ingress IP"
        )
    candidate_url = f"http://{parts[0]}:{WEB_CONSOLE_SERVICE_PORT}"
    return validate_private_web_console_url(candidate_url)


def _wait_for_web_console_health(
    base_url: str,
    *,
    process: subprocess.Popen[str] | None = None,
    timeout: float = WEB_CONSOLE_READY_TIMEOUT_SECONDS,
) -> None:
    """Wait until ``GET {base_url}/healthz`` returns HTTP 200."""

    health_url = f"{validate_private_web_console_url(base_url)}/healthz"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise PortalError(
                "kubectl port-forward for web console exited during startup "
                f"with code {process.returncode}"
            )
        try:
            with urllib.request.urlopen(health_url, timeout=2.0) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    raise PortalError(
        f"web console did not become healthy at {health_url} within "
        f"{timeout:g} seconds"
    )


@contextmanager
def _port_forward_web_console(
    *,
    namespace: str,
    kube_context: str,
) -> Iterator[str]:
    """Open a loopback ``kubectl port-forward`` tunnel to the Web Console Service."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_sock:
        probe_sock.bind(("127.0.0.1", 0))
        local_port = probe_sock.getsockname()[1]

    cmd = _kubectl_cmd(
        "port-forward",
        f"svc/{WEB_CONSOLE_SERVICE_NAME}",
        f"{local_port}:{WEB_CONSOLE_SERVICE_PORT}",
        "--address",
        "127.0.0.1",
        namespace=namespace,
        kube_context=kube_context,
    )
    process = subprocess.Popen(
        cmd,
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    endpoint = f"http://127.0.0.1:{local_port}"
    try:
        _wait_for_web_console_health(endpoint, process=process)
        yield endpoint
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.stderr is not None:
            process.stderr.close()


@contextmanager
def managed_web_console_endpoint() -> Iterator[str]:
    """Manage cluster/Web Console setup, private endpoint validation, and teardown.

    Supported modes via ``CUJ_WEB_CONSOLE_CLUSTER_MODE``:
    - ``create``: Stand up an ephemeral GKE cluster with ``install.sh`` and the
      Web Console enabled, connect via loopback ``kubectl port-forward`` or a
      private IP (``CUJ_WEB_CONSOLE_USE_PRIVATE_IP=true``), and tear the
      cluster down on exit via ``uninstall.sh`` unless deployed via private IP
      with ``CUJ_KEEP_PRIVATE_WEB_CONSOLE=true``.
    - ``existing``: Deploy the Web Console onto an existing cluster, connect
      via loopback ``kubectl port-forward`` or a private IP
      (``CUJ_WEB_CONSOLE_USE_PRIVATE_IP=true``), and tear the Web Console down
      on exit unless deployed via private IP (and ``CUJ_TEARDOWN_WEB_CONSOLE``
      is not ``true``).
    - ``port-forward``: Forward an already-deployed ``svc/kube-agents-web-console``
      on ``127.0.0.1`` and tear down on exit when
      ``CUJ_TEARDOWN_WEB_CONSOLE=true``.
    """

    mode = os.environ.get(WEB_CONSOLE_CLUSTER_MODE_ENV, "").strip().casefold()
    configured_url = os.environ.get(WEB_CONSOLE_URL_ENV, "").strip()
    if not mode and not configured_url:
        yield ""
        return

    if mode and mode not in VALID_CLUSTER_MODES:
        raise PortalError(
            f"Unsupported {WEB_CONSOLE_CLUSTER_MODE_ENV}={mode!r}; "
            f"expected one of {sorted(VALID_CLUSTER_MODES)}"
        )

    namespace = (
        os.environ.get(KUBE_NAMESPACE_ENV, "").strip()
        or DEFAULT_KUBE_NAMESPACE
    )
    kube_context = os.environ.get(KUBE_CONTEXT_ENV, "").strip()
    project_id = os.environ.get(PROJECT_ID_ENV, "").strip()
    location = (
        os.environ.get(CLUSTER_LOCATION_ENV, "").strip()
        or DEFAULT_CLUSTER_LOCATION
    )
    model_provider = (
        os.environ.get(MODEL_PROVIDER_ENV, "").strip()
        or DEFAULT_MODEL_PROVIDER
    )
    use_private_ip = _env_truthy(WEB_CONSOLE_USE_PRIVATE_IP_ENV)
    keep_private = _env_truthy(KEEP_PRIVATE_WEB_CONSOLE_ENV)
    explicit_teardown = _env_truthy(TEARDOWN_WEB_CONSOLE_ENV)

    if not mode:
        validated = validate_private_web_console_url(configured_url)
        try:
            yield validated
        finally:
            if explicit_teardown:
                _teardown_web_console_on_existing_cluster(
                    namespace=namespace,
                    kube_context=kube_context,
                )
        return

    cluster_name = os.environ.get(CLUSTER_NAME_ENV, "").strip()
    explicit_cluster_name = bool(cluster_name)
    if mode == "create":
        if not project_id:
            raise PortalError(
                f"{PROJECT_ID_ENV} is required when "
                f"{WEB_CONSOLE_CLUSTER_MODE_ENV}='create'"
            )
        if explicit_cluster_name and _cluster_already_exists(
            project_id=project_id,
            cluster_name=cluster_name,
            location=location,
        ):
            raise PortalError(
                f"Refusing to run {WEB_CONSOLE_CLUSTER_MODE_ENV}='create' "
                f"against pre-existing cluster {cluster_name!r} in project "
                f"{project_id!r}; use {WEB_CONSOLE_CLUSTER_MODE_ENV}='existing' "
                "or 'port-forward' so teardown does not delete an existing cluster."
            )
        if not cluster_name:
            cluster_name = f"{EPHEMERAL_CLUSTER_PREFIX}{os.urandom(4).hex()}"

    previous_url = os.environ.get(WEB_CONSOLE_URL_ENV)
    provisioned_cluster = False
    deployed_existing = False
    try:
        if mode == "create":
            try:
                _provision_cluster_with_web_console(
                    project_id=project_id,
                    cluster_name=cluster_name,
                    location=location,
                    model_provider=model_provider,
                )
            except BaseException:
                if _cluster_already_exists(
                    project_id=project_id,
                    cluster_name=cluster_name,
                    location=location,
                ):
                    provisioned_cluster = True
                raise
            else:
                provisioned_cluster = True
        elif mode == "existing":
            _deploy_web_console_on_existing_cluster(
                project_id=project_id,
                cluster_name=cluster_name,
                location=location,
                model_provider=model_provider,
                namespace=namespace,
                kube_context=kube_context,
            )
            deployed_existing = True

        with ExitStack() as stack:
            if configured_url:
                endpoint = validate_private_web_console_url(configured_url)
            elif use_private_ip and mode in {"create", "existing"}:
                endpoint = _resolve_private_web_console_url(
                    namespace=namespace,
                    kube_context=kube_context,
                )
            else:
                endpoint = stack.enter_context(
                    _port_forward_web_console(
                        namespace=namespace,
                        kube_context=kube_context,
                    )
                )
            os.environ[WEB_CONSOLE_URL_ENV] = endpoint
            yield endpoint
    finally:
        if previous_url is None:
            os.environ.pop(WEB_CONSOLE_URL_ENV, None)
        else:
            os.environ[WEB_CONSOLE_URL_ENV] = previous_url

        if mode == "create" and provisioned_cluster:
            if not (use_private_ip and keep_private and not explicit_teardown):
                _teardown_cluster(
                    project_id=project_id,
                    cluster_name=cluster_name,
                    location=location,
                )
        elif mode == "existing" and (deployed_existing or explicit_teardown):
            should_teardown = explicit_teardown or not use_private_ip
            if should_teardown and not (
                use_private_ip and keep_private and not explicit_teardown
            ):
                _teardown_web_console_on_existing_cluster(
                    namespace=namespace,
                    kube_context=kube_context,
                )
        elif mode == "port-forward" and explicit_teardown:
            _teardown_web_console_on_existing_cluster(
                namespace=namespace,
                kube_context=kube_context,
            )


def _fetch_web_console_transcript(
    transport: Portal,
    session_id: str,
) -> dict[str, Any]:
    """Fetch ``GET /api/sessions/{id}/transcript`` from the Web Console via ``Portal``."""

    quoted = urllib.parse.quote(session_id, safe="")
    return transport.get(f"api/sessions/{quoted}/transcript")


def _run_web_console_turn(
    base_url: str,
    prompt: str,
    log: EvidenceLog,
) -> dict[str, Any]:
    """Execute one synchronous turn against the Web Console HTTP API."""

    root = validate_private_web_console_url(base_url)
    session_id = (
        f"{WEB_CONSOLE_SESSION_PREFIX}"
        f"{os.urandom(WEB_CONSOLE_RAND_BYTES).hex()}"
    )
    budget_s = max_latency_seconds()
    timeout_s = _positive_env_float(
        TURN_TIMEOUT_ENV, max(DEFAULT_TURN_TIMEOUT_SECONDS, budget_s * 2)
    )
    poll_s = _positive_env_float(
        POLL_INTERVAL_ENV, TRANSCRIPT_POLL_INTERVAL_SECONDS
    )
    request_payload = {"message": prompt, "session_id": session_id}
    log.record("request", {"endpoint": root, **request_payload})

    started = time.monotonic()
    deadline = started + timeout_s
    chat_transport = _web_console_transport(root, timeout=timeout_s)
    chat_payload: dict[str, Any] = {}
    chat_timed_out = False
    try:
        chat_payload = chat_transport.post("api/chat", request_payload)
    except PortalError as exc:
        cause = exc.__cause__
        if isinstance(cause, TimeoutError) or (
            cause is not None and "timed out" in str(cause).casefold()
        ):
            chat_timed_out = True
        else:
            raise

    transcript_transport = _web_console_transport(
        root,
        timeout=TRANSCRIPT_TIMEOUT_SECONDS,
    )
    transcript_payload = _fetch_web_console_transcript(
        transcript_transport, session_id
    )
    settle_deadline = min(
        deadline + TRANSCRIPT_SETTLE_GRACE_SECONDS,
        time.monotonic() + TRANSCRIPT_SETTLE_TIMEOUT_SECONDS,
    )
    settled = _transcript_turn_settled(transcript_payload)
    while not settled and time.monotonic() < settle_deadline:
        time.sleep(poll_s)
        transcript_payload = _fetch_web_console_transcript(
            transcript_transport, session_id
        )
        settled = _transcript_turn_settled(transcript_payload)

    if chat_timed_out and not (transcript_payload.get("messages") or []):
        raise PortalError(
            "web console POST /api/chat timed out without transcript"
        )

    latency_s = time.monotonic() - started

    interaction = _project_web_console_interaction(
        prompt,
        session_id,
        chat_payload,
        transcript_payload,
        latency_seconds=latency_s,
        budget_seconds=budget_s,
    )
    log.record("interaction", {"poll": 0, "value": interaction})
    log.record(
        "interaction_final",
        {
            "poll": 0,
            "status": interaction.get("status"),
            "latencySeconds": interaction.get("latencySeconds"),
            "maxLatencySeconds": interaction.get("maxLatencySeconds"),
        },
    )
    return interaction


def run_harness_scenario(scenario: Scenario) -> None:
    """Run ``scenario`` via a private Web Console endpoint or ``Scenario.run_test``."""

    web_console_url = os.environ.get(WEB_CONSOLE_URL_ENV, "").strip()
    cluster_mode = os.environ.get(WEB_CONSOLE_CLUSTER_MODE_ENV, "").strip()
    if not web_console_url and not cluster_mode:
        scenario.run_test()
        return

    log = EvidenceLog.temporary(f"kube-agents-{scenario.id}-")
    try:
        with ExitStack() as stack:
            if not web_console_url:
                web_console_url = stack.enter_context(
                    managed_web_console_endpoint()
                )
            prompt = scenario.build_prompt()
            interaction = _run_web_console_turn(web_console_url, prompt, log)
        acceptance = scenario.evaluate_acceptance(interaction)
        milestones = None
        milestone_results = ()
        milestone_summary = None
        milestone_error = ""
        if scenario.evaluate_milestones is not None:
            try:
                milestones = scenario.evaluate_milestones(interaction)
                milestone_results = milestones.results
                milestone_summary = milestones.summary()
            except Exception as exc:
                milestones = None
                milestone_error = f"{type(exc).__name__}: {exc}"
                log.record("milestone_error", milestone_error)
        for result in milestone_results:
            log.record("milestone", result.to_dict())
        for result in acceptance.results:
            log.record("acceptance_criterion", result.to_dict())
        transcript = None
        try:
            transcript = log.write_transcript(prompt, interaction)
        except Exception as exc:
            log.record("transcript_error", f"{type(exc).__name__}: {exc}")
        log.record(
            "summary",
            {
                "interaction": {
                    "status": interaction.get("status"),
                    "latencySeconds": interaction.get("latencySeconds"),
                    "maxLatencySeconds": interaction.get("maxLatencySeconds"),
                    "error": interaction.get("error") or None,
                    "diagnostics": interaction.get("diagnostics") or None,
                },
                "acceptanceCriteria": acceptance.summary(),
                "milestones": milestone_summary,
                "milestoneError": milestone_error or None,
            },
        )
    except (OSError, ValueError, PortalError) as exc:
        pytest.fail(f"{scenario.id} setup failed: {exc}; evidence: {log.path}")

    status = str(interaction.get("status") or "unknown").upper()
    latency_s = interaction.get("latencySeconds")
    budget_s = interaction.get("maxLatencySeconds")
    print(
        f"Interaction outcome: {status} "
        f"(latency: {latency_s}s / budget: {budget_s}s)"
    )
    if milestones is not None:
        print("Milestones (diagnostic only):")
        for line in milestones.report_lines():
            print(line)
    elif milestone_error:
        print(f"Milestones unavailable (diagnostic only): {milestone_error}")
    print("Acceptance criteria (test outcome):")
    for line in acceptance.report_lines():
        print(line)
    print(f"Evidence: {log.path}")
    if transcript is not None:
        print(f"Conversation: {transcript}")
    interaction_failure = ""
    if interaction.get("status") != COMPLETED_STATUS:
        error = interaction.get("error") or "interaction did not complete"
        interaction_failure = f"status={interaction.get('status')}: {error}; "
    assert acceptance.passed, (
        f"{interaction_failure}"
        "acceptance criteria not met: "
        f"{acceptance.failure_summary()}; evidence: {log.path}"
    )
