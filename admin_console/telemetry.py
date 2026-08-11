"""Bounded, read-only Cloud Logging and Cloud Trace activity provider."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from admin_console.connections import CommandResult, CommandRunner, GcloudRunner
from admin_console.domain import (
    ActivityEvent,
    AttributionLevel,
    TriggerKind,
)
from admin_console.project_config import (
    is_valid_cluster_name,
    is_valid_namespace,
    is_valid_project_id,
)

_WRAPPED_AUDIT = re.compile(r"^(?P<prefix>.*?):\s*(?P<payload>\{.*\})\s*$")
_CONTEXT = re.compile(r"\[(?P<context>[^\]]+)\]")
_SECRET_NAME = (
    r"(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|private[_-]?key|ssh[_-]?key|credential|password|secret|token)"
)
_SECRET_PATTERNS = (
    re.compile(
        r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s\"',}]+"
    ),
    re.compile(
        rf"(?i)((?:{_SECRET_NAME})[\"']?\s*[:=]\s*[\"']?)[^\"'\s,}}]+"
    ),
)
_DETAIL_LIMIT = 8_000
MAX_TRACE_PAGES = 10
_SECRET_KEY = re.compile(rf"(?i){_SECRET_NAME}")


@dataclass(frozen=True)
class TelemetrySourceState:
    name: str
    status: str
    records_read: int
    truncated: bool
    detail: str
    pages_read: int = 0


@dataclass(frozen=True)
class TelemetrySnapshot:
    project_id: str
    cluster: str
    start_time: datetime
    end_time: datetime
    loaded_at: datetime
    events: tuple[ActivityEvent, ...]
    sources: tuple[TelemetrySourceState, ...]

    @property
    def incomplete(self) -> bool:
        return any(
            source.status == "error" or source.truncated for source in self.sources
        )


def _parse_time(value: object, fallback: datetime) -> datetime:
    if not isinstance(value, str) or not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    return parsed.astimezone(UTC)


def _duration_ms(start: datetime, end: datetime) -> int:
    return max(0, round((end - start).total_seconds() * 1000))


def _coerce_duration_ms(value: object) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def redact_evidence(value: object) -> str:
    """Redact common credential forms and cap evidence rendered by the portal."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = value
    else:
        parsed = value

    def scrub(item: object) -> object:
        if isinstance(item, dict):
            return {
                str(key): "[REDACTED]" if _SECRET_KEY.search(str(key)) else scrub(child)
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [scrub(child) for child in item]
        return item

    parsed = scrub(parsed)
    if isinstance(parsed, (dict, list)):
        rendered = json.dumps(parsed, sort_keys=True, ensure_ascii=False)
    else:
        rendered = _text(parsed)
    for pattern in _SECRET_PATTERNS:
        rendered = pattern.sub(r"\1[REDACTED]", rendered)
    if len(rendered) > _DETAIL_LIMIT:
        return rendered[:_DETAIL_LIMIT] + "\n… [truncated by portal]"
    return rendered


def _first(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value is not None and str(value):
            return str(value)
    return ""


def _status(value: str) -> str:
    lowered = value.lower()
    if lowered in {"ok", "success", "succeeded", "complete", "completed", "allowed"}:
        return "completed"
    if lowered in {"error", "failed", "failure", "denied", "timeout", "timed_out"}:
        return "failed"
    if lowered in {"blocked", "rejected"}:
        return "blocked"
    if lowered in {"start", "started", "running", "pending", "requested"}:
        return "running"
    return lowered or "completed"


def _event_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256(
        "\x1f".join(str(part) for part in parts).encode("utf-8")
    ).hexdigest()[:20]
    return f"{prefix}-{digest}"


def _logging_payload(row: dict[str, Any]) -> tuple[dict[str, Any], str]:
    direct = row.get("jsonPayload")
    if isinstance(direct, dict) and direct.get("audit_event"):
        return direct, ""
    candidate = ""
    if isinstance(direct, dict):
        candidate = _text(direct.get("log"))
    if not candidate:
        candidate = _text(row.get("textPayload"))
    match = _WRAPPED_AUDIT.match(candidate)
    if not match:
        return {}, ""
    try:
        payload = json.loads(match.group("payload"))
    except json.JSONDecodeError:
        return {}, match.group("prefix")
    return (payload if isinstance(payload, dict) else {}), match.group("prefix")


def _logging_trigger(
    payload: dict[str, Any], context: str
) -> tuple[TriggerKind, AttributionLevel]:
    explicit = _first(payload, "trigger_kind", "trigger.kind")
    if explicit in {kind.value for kind in TriggerKind}:
        return TriggerKind(explicit), AttributionLevel.EXPLICIT
    if context.startswith("cron_"):
        return TriggerKind.CRON, AttributionLevel.INHERITED
    if _first(payload, "user_id", "user.id"):
        return TriggerKind.HUMAN, AttributionLevel.EXPLICIT
    if _first(payload, "platform") in {"web", "google_chat", "slack"}:
        return TriggerKind.HUMAN, AttributionLevel.EXPLICIT
    return TriggerKind.UNKNOWN, AttributionLevel.MISSING


def normalize_logging_row(
    row: dict[str, Any], project_id: str
) -> ActivityEvent | None:
    """Normalize one supported application audit log record."""
    payload, prefix = _logging_payload(row)
    audit_event = _first(payload, "audit_event")
    if not audit_event:
        return None

    resource = row.get("resource") if isinstance(row.get("resource"), dict) else {}
    labels = (
        resource.get("labels")
        if isinstance(resource.get("labels"), dict)
        else {}
    )
    context_match = _CONTEXT.search(prefix)
    context = context_match.group("context") if context_match else ""
    trigger, attribution = _logging_trigger(payload, context)
    timestamp = _parse_time(
        payload.get("occurred_at"),
        _parse_time(row.get("timestamp"), datetime.now(UTC)),
    )
    insert_id = _first(row, "insertId") or _event_id(
        "log", timestamp.isoformat(), audit_event, payload
    )
    session_id = _first(payload, "session_id", "session.id") or context
    task_id = _first(payload, "task_id")
    message_hash = _first(payload, "message_sha256")
    interaction_id = (
        _first(payload, "interaction_id", "interaction.id")
        or (f"message:{session_id}:{message_hash}" if session_id and message_hash else "")
        or task_id
        or context
        or f"log:{insert_id}"
    )
    tool_name = _first(payload, "tool_name")
    action_type = "audit"
    action_name = audit_event.replace("_", " ").title()
    status = _status(_first(payload, "status"))
    summary = f"Observed {action_name.lower()} in Cloud Logging."
    if audit_event == "tool_call_start":
        action_type, status = "tool", "running"
        action_name = f"Start {tool_name or 'tool'}"
        summary = f"{tool_name or 'Tool'} execution started."
    elif audit_event == "tool_call_end":
        action_type = "tool"
        action_name = f"Finish {tool_name or 'tool'}"
        result_value = payload.get("result")
        if isinstance(result_value, str):
            try:
                result_value = json.loads(result_value)
            except json.JSONDecodeError:
                pass
        failed = bool(
            isinstance(result_value, dict)
            and (
                result_value.get("error")
                or (
                    result_value.get("exit_code") is not None
                    and result_value.get("exit_code") != 0
                )
            )
        )
        status = "failed" if failed else "completed"
        summary = f"{tool_name or 'Tool'} execution finished."
    elif audit_event.startswith("approval_"):
        action_type = "approval"
        if audit_event == "approval_request":
            status, summary = "running", "Human approval was requested."
        else:
            choice = _first(payload, "choice")
            status = (
                "blocked"
                if choice.lower() in {"deny", "denied", "reject", "rejected", "cancel"}
                else "completed"
            )
            summary = f"Approval response recorded{f': {choice}' if choice else ''}."

    source_url = (
        "https://console.cloud.google.com/logs/query;"
        f"query=insertId%3D%22{urllib.parse.quote(insert_id, safe='')}%22"
        f"?project={urllib.parse.quote(project_id, safe='')}"
    )
    details = {
        "source": "cloud_logging",
        "source_record_id": insert_id,
        "evidence_url": source_url,
        "audit_event": audit_event,
        "log_name": _first(row, "logName"),
    }
    for source_key, detail_key in (
        ("args", "tool_arguments"),
        ("result", "tool_result"),
        ("command", "command"),
        ("description", "approval_description"),
        ("choice", "approval_choice"),
        ("message_sha256", "message_sha256"),
        ("message_length", "message_length"),
    ):
        if payload.get(source_key) is not None:
            details[detail_key] = redact_evidence(payload[source_key])

    return ActivityEvent(
        event_id=f"log-{insert_id}",
        occurred_at=timestamp,
        interaction_id=interaction_id,
        trigger_kind=trigger,
        action_type=action_type,
        action_name=action_name,
        status=status,
        summary=summary,
        agent_name=_first(payload, "agent_profile", "agent_name")
        or _first(labels, "container_name")
        or "platform-agent",
        platform=_first(payload, "platform"),
        user_id=_first(payload, "user_id", "user.id"),
        session_id=session_id,
        task_id=task_id,
        tool_name=tool_name,
        cluster=_first(payload, "cluster") or _first(labels, "cluster_name"),
        namespace=_first(payload, "namespace") or _first(labels, "namespace_name"),
        resource=_first(payload, "resource"),
        duration_ms=_coerce_duration_ms(payload.get("duration_ms")),
        attribution=attribution,
        trace_id=_first(payload, "trace_id", "trace.id"),
        details=details,
    )


def _trace_trigger(labels: dict[str, Any], session_id: str) -> TriggerKind:
    kind = _first(labels, "trigger.kind", "hermes.session.kind")
    if kind == "cron" or session_id.startswith("cron_"):
        return TriggerKind.CRON
    if kind == "event":
        return TriggerKind.EVENT
    if kind == "retry":
        return TriggerKind.RETRY
    if kind in {"agent_followup", "followup"}:
        return TriggerKind.AGENT_FOLLOWUP
    if _first(labels, "user.id", "hermes.sender.id", "chat.platform"):
        return TriggerKind.HUMAN
    return TriggerKind.UNKNOWN


def _span_classification(
    name: str, labels: dict[str, Any]
) -> tuple[str, str, str]:
    if name == "cron":
        return "cron", "Scheduled agent run", _status(_first(labels, "hermes.turn.final_status"))
    if name == "agent":
        return "agent_turn", "Agent turn", _status(_first(labels, "hermes.turn.final_status"))
    if name.startswith("tool."):
        tool = name.removeprefix("tool.")
        return "tool", f"Use {tool}", _status(_first(labels, "hermes.tool.outcome"))
    if name.startswith("skill."):
        skill = name.removeprefix("skill.")
        return "skill", f"Load {skill}", _status(_first(labels, "hermes.skill.result_status"))
    if name.startswith("approval."):
        granted = _first(labels, "hermes.approval.granted")
        choice = _first(labels, "hermes.approval.choice").lower()
        status = (
            "blocked"
            if granted.lower() == "false"
            or choice in {"deny", "denied", "reject", "rejected", "cancel"}
            else "completed"
        )
        return "approval", name.removeprefix("approval.").title(), status
    if name.startswith(("llm.", "api.")):
        finish = _first(labels, "llm.response.finish_reason")
        return "model", name, "failed" if finish.lower() == "error" else "completed"
    return "span", name or "Unnamed span", "completed"


def normalize_trace(
    trace: dict[str, Any],
    project_id: str,
    session_users: dict[str, str] | None = None,
) -> list[ActivityEvent]:
    """Normalize supported Hermes spans while retaining source IDs and links."""
    trace_id = _first(trace, "traceId")
    spans = trace.get("spans")
    if not trace_id or not isinstance(spans, list):
        return []
    session_users = session_users or {}

    trace_labels: list[dict[str, Any]] = [
        span.get("labels") if isinstance(span.get("labels"), dict) else {}
        for span in spans
        if isinstance(span, dict)
    ]
    root_user = next(
        (
            _first(labels, "user.id", "hermes.sender.id")
            for labels in trace_labels
            if _first(labels, "user.id", "hermes.sender.id")
        ),
        "",
    )
    events: list[ActivityEvent] = []
    for span in spans:
        if not isinstance(span, dict):
            continue
        labels = span.get("labels") if isinstance(span.get("labels"), dict) else {}
        session_id = _first(
            labels,
            "session.id",
            "session_id",
            "hermes.session_id",
            "gen_ai.conversation.id",
        )
        if not session_id:
            continue
        name = _first(span, "name")
        action_type, action_name, status = _span_classification(name, labels)
        start = _parse_time(span.get("startTime"), datetime.now(UTC))
        end = _parse_time(span.get("endTime"), start)
        trigger = _trace_trigger(labels, session_id)
        user_id = _first(labels, "user.id", "hermes.sender.id") or root_user
        if not user_id:
            user_id = session_users.get(session_id, "")
            if user_id:
                trigger = TriggerKind.HUMAN
        if _first(labels, "interaction.id"):
            attribution = AttributionLevel.EXPLICIT
        elif user_id or trigger != TriggerKind.UNKNOWN:
            attribution = AttributionLevel.INHERITED
        else:
            attribution = AttributionLevel.MISSING
        span_id = _first(span, "spanId") or _event_id(
            "span", trace_id, name, start.isoformat()
        )
        tool_name = _first(labels, "tool.name", "gen_ai.tool.name")
        if not tool_name and action_type == "tool":
            tool_name = name.removeprefix("tool.")
        summary = {
            "agent_turn": "The agent processed one persisted session turn.",
            "cron": "A scheduled agent run was recorded.",
            "tool": f"The agent invoked {tool_name or 'a tool'}.",
            "skill": "The agent loaded a skill.",
            "approval": "A tool approval decision was recorded.",
            "model": "The agent called its configured model.",
        }.get(action_type, "An agent span was recorded.")
        evidence_url = (
            "https://console.cloud.google.com/traces/list"
            f"?project={urllib.parse.quote(project_id, safe='')}"
            f"&tid={urllib.parse.quote(trace_id, safe='')}"
        )
        details = {
            "source": "cloud_trace",
            "source_record_id": f"{trace_id}/{span_id}",
            "evidence_url": evidence_url,
            "span_name": name,
            "span_id": span_id,
            "parent_span_id": _first(span, "parentSpanId"),
            "correlation_id": _first(labels, "correlation.id"),
        }
        for keys, detail_key in (
            (("input.value", "gen_ai.input.messages"), "input"),
            (("output.value", "gen_ai.output.messages"), "output"),
            (("hermes.tool.command",), "tool_command"),
            (("hermes.tool.target",), "tool_target"),
            (("gen_ai.tool.call.arguments",), "tool_arguments"),
            (("gen_ai.tool.call.result",), "tool_result"),
            (("hermes.approval.command",), "approval_command"),
            (("hermes.approval.choice",), "approval_choice"),
            (("hermes.turn.tools",), "turn_tools"),
            (("hermes.turn.tool_outcomes",), "turn_tool_outcomes"),
        ):
            value = _first(labels, *keys)
            if value:
                details[detail_key] = redact_evidence(value)
        events.append(
            ActivityEvent(
                event_id=f"trace-{trace_id}-{span_id}",
                occurred_at=start,
                interaction_id=_first(labels, "interaction.id") or trace_id,
                trigger_kind=trigger,
                action_type=action_type,
                action_name=action_name,
                status=status,
                summary=summary,
                agent_name=_first(
                    labels,
                    "kubeagents.agent_name",
                    "gen_ai.agent.name",
                    "service.name",
                )
                or "agent",
                platform=_first(labels, "chat.platform"),
                user_id=user_id,
                session_id=session_id,
                task_id=_first(labels, "task.id"),
                parent_task_id=_first(labels, "parent_task.id"),
                tool_name=tool_name,
                cluster=_first(labels, "k8s.cluster.name"),
                namespace=_first(labels, "k8s.namespace.name"),
                resource=_first(labels, "hermes.tool.target"),
                duration_ms=_duration_ms(start, end),
                attribution=attribution,
                trace_id=trace_id,
                details=details,
            )
        )
    return events


class CloudTelemetryProvider:
    """Load a bounded snapshot from Cloud Logging and Cloud Trace."""

    def __init__(
        self,
        project_id: str,
        *,
        account: str = "",
        cluster: str = "",
        namespace: str = "kubeagents-system",
        hours: int = 24,
        log_limit: int = 1_000,
        trace_limit: int = 100,
        trace_pages: int = 1,
        runner: CommandRunner | None = None,
    ) -> None:
        if not is_valid_project_id(project_id):
            raise ValueError("invalid Google Cloud project ID")
        if cluster and not is_valid_cluster_name(cluster):
            raise ValueError("invalid GKE cluster name")
        if not is_valid_namespace(namespace):
            raise ValueError("invalid Kubernetes namespace")
        if hours not in {1, 6, 24, 72, 168, 720}:
            raise ValueError("unsupported telemetry window")
        if not 1 <= trace_pages <= MAX_TRACE_PAGES:
            raise ValueError("trace pages must be between 1 and 10")
        self.project_id = project_id
        self.cluster = cluster
        self.namespace = namespace
        self.hours = hours
        self.log_limit = log_limit
        self.trace_limit = trace_limit
        self.trace_pages = trace_pages
        self.runner = runner or GcloudRunner(account)
        self._snapshot: TelemetrySnapshot | None = None

    def list_activity(self) -> list[ActivityEvent]:
        return list(self.get_snapshot().events)

    def get_snapshot(self) -> TelemetrySnapshot:
        if self._snapshot is None:
            self._snapshot = self._load()
        return self._snapshot

    def _load_logging(
        self,
    ) -> tuple[list[ActivityEvent], TelemetrySourceState, dict[str, str]]:
        base = (
            'resource.type="k8s_container" '
            f'AND resource.labels.namespace_name="{self.namespace}"'
        )
        if self.cluster:
            base += f' AND resource.labels.cluster_name="{self.cluster}"'
        # Separate indexed payload variants instead of one broad OR query. This
        # is materially faster in Cloud Logging and keeps each read bounded.
        queries = (
            f'({base}) AND jsonPayload.log:"audit_event"',
            f"({base}) AND jsonPayload.audit_event:*",
        )

        def read(query: str) -> CommandResult:
            return self.runner.run(
                [
                    "logging",
                    "read",
                    query,
                    f"--project={self.project_id}",
                    f"--freshness={self.hours}h",
                    f"--limit={self.log_limit}",
                    "--order=desc",
                    "--format=json",
                ],
                timeout=20,
            )

        with ThreadPoolExecutor(max_workers=len(queries)) as executor:
            results = list(executor.map(read, queries))
        failures = [result for result in results if result.returncode != 0]
        if failures:
            detail = "Cloud Logging read failed."
            if any(result.timed_out for result in failures):
                detail = "Cloud Logging read timed out."
            elif any(
                "permission" in result.stderr.lower() or "403" in result.stderr
                for result in failures
            ):
                detail = "Cloud Logging permission was denied."
            return [], TelemetrySourceState(
                "Cloud Logging", "error", 0, False, detail
            ), {}
        rows_by_id: dict[str, dict[str, Any]] = {}
        for result in results:
            try:
                result_rows = json.loads(result.stdout or "[]")
            except json.JSONDecodeError:
                return [], TelemetrySourceState(
                    "Cloud Logging",
                    "error",
                    0,
                    False,
                    "Cloud Logging returned invalid JSON.",
                ), {}
            if not isinstance(result_rows, list):
                continue
            for row in result_rows:
                if not isinstance(row, dict):
                    continue
                key = _first(row, "insertId") or _event_id("row", row)
                rows_by_id[key] = row
        rows = list(rows_by_id.values())
        events = [
            event
            for row in rows
            if isinstance(row, dict)
            if (event := normalize_logging_row(row, self.project_id)) is not None
        ]
        session_users = {
            event.session_id: event.user_id
            for event in events
            if event.session_id and event.user_id
        }
        truncated = any(
            len(json.loads(result.stdout or "[]")) >= self.log_limit
            for result in results
        )
        status = "ready" if events else "empty"
        detail = (
            f"Read {len(rows)} matching audit record(s)."
            if rows
            else "No matching audit records were found in this window."
        )
        return events, TelemetrySourceState(
            "Cloud Logging", status, len(rows), truncated, detail
        ), session_users

    def _load_trace(
        self,
        start: datetime,
        end: datetime,
        session_users: dict[str, str],
    ) -> tuple[list[ActivityEvent], TelemetrySourceState]:
        token_result = self.runner.run(
            ["auth", "application-default", "print-access-token"], timeout=15
        )
        token = token_result.stdout.strip()
        if token_result.returncode != 0 or not token:
            return [], TelemetrySourceState(
                "Cloud Trace",
                "error",
                0,
                False,
                "Application Default Credentials are unavailable.",
            )
        terms = ["label:session.id"]
        if self.cluster:
            terms.append(f"+k8s.cluster.name:{self.cluster}")
        traces: list[dict[str, Any]] = []
        next_page_token = ""
        pages_read = 0
        read_error = ""
        try:
            for page_number in range(1, self.trace_pages + 1):
                parameters = {
                    "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "pageSize": self.trace_limit,
                    "view": "COMPLETE",
                    "orderBy": "start desc",
                    "filter": " ".join(terms),
                }
                if next_page_token:
                    parameters["pageToken"] = next_page_token
                query = urllib.parse.urlencode(parameters)
                request = urllib.request.Request(
                    f"https://cloudtrace.googleapis.com/v1/projects/"
                    f"{self.project_id}/traces?{query}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                )
                try:
                    with urllib.request.urlopen(request, timeout=30) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                except urllib.error.HTTPError as exc:
                    read_error = (
                        "Cloud Trace permission was denied."
                        if exc.code in {401, 403}
                        else f"Cloud Trace returned HTTP {exc.code}."
                    )
                    break
                except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
                    read_error = "Cloud Trace could not be read."
                    break

                page_traces = (
                    payload.get("traces", []) if isinstance(payload, dict) else []
                )
                if not isinstance(page_traces, list):
                    page_traces = []
                traces.extend(
                    trace for trace in page_traces if isinstance(trace, dict)
                )
                pages_read = page_number
                next_page_token = (
                    str(payload.get("nextPageToken") or "")
                    if isinstance(payload, dict)
                    else ""
                )
                if not next_page_token:
                    break
        finally:
            token = ""
            token_result = CommandResult(0)

        events = [
            event
            for trace in traces
            for event in normalize_trace(trace, self.project_id, session_users)
        ]
        if read_error:
            detail = read_error
            if traces:
                detail = (
                    f"Read {len(traces)} matching trace(s) across {pages_read} page(s) "
                    f"before a later page failed. {read_error}"
                )
            return events, TelemetrySourceState(
                "Cloud Trace",
                "error",
                len(traces),
                bool(next_page_token),
                detail,
                pages_read,
            )

        truncated = bool(next_page_token)
        status = "ready" if events else "empty"
        detail = (
            f"Read {len(traces)} matching trace(s) and {len(events)} span(s) "
            f"across {pages_read} page(s)."
            if traces
            else "No session-attributed traces were found in this window."
        )
        return events, TelemetrySourceState(
            "Cloud Trace", status, len(traces), truncated, detail, pages_read
        )

    def _load(self) -> TelemetrySnapshot:
        end = datetime.now(UTC)
        start = end - timedelta(hours=self.hours)
        with ThreadPoolExecutor(max_workers=2) as executor:
            logging_future = executor.submit(self._load_logging)
            trace_future = executor.submit(self._load_trace, start, end, {})
            log_events, log_state, session_users = logging_future.result()
            trace_events, trace_state = trace_future.result()
        trace_events = [
            replace(
                event,
                user_id=session_users[event.session_id],
                trigger_kind=TriggerKind.HUMAN,
                attribution=AttributionLevel.INHERITED,
            )
            if (
                not event.user_id
                and event.session_id
                and event.session_id in session_users
            )
            else event
            for event in trace_events
        ]
        events = sorted(
            [*log_events, *trace_events],
            key=lambda event: event.occurred_at,
            reverse=True,
        )
        return TelemetrySnapshot(
            self.project_id,
            self.cluster,
            start,
            end,
            datetime.now(UTC),
            tuple(events),
            (log_state, trace_state),
        )
