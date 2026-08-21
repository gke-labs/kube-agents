"""Typed client shared by the Streamlit Chat page and external callers."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from fastapi.testclient import TestClient

from admin_console.agent_chat import MAX_HISTORY_MESSAGES
from admin_console.agent_runtime import (
    AgentConversation,
    AgentMessage,
    AgentTaskUpdate,
    HistoryResult,
    MessageResult,
    TaskUpdateResult,
)
from admin_console.api.authorization import portal_api_headers
from admin_console.api.app import create_app, target_runtime_factory
from admin_console.chat.backend import RuntimeChatBackend
from admin_console.chat.service import ChatService
from admin_console.project_config import DeploymentTarget, deployment_target_headers
from admin_console.runtime_contract import canonical_platform_agent_name


class Response(Protocol):
    status_code: int

    def json(self) -> Any: ...


class Transport(Protocol):
    def get(self, url: str, **kwargs) -> Response: ...

    def post(self, url: str, **kwargs) -> Response: ...


@dataclass(frozen=True)
class InteractionView:
    interaction_id: str
    session_id: str
    status: str
    terminal: bool
    root_run_id: str = ""
    output: str = ""
    error: str = ""
    diagnostics: tuple[str, ...] = ()
    approval: dict[str, Any] | None = None
    tasks: tuple[dict[str, Any], ...] = ()
    tool_calls: tuple[dict[str, Any], ...] = ()


class PortalApiError(RuntimeError):
    def __init__(self, message: str, guidance: str = "") -> None:
        super().__init__(message)
        self.guidance = guidance


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class PortalApiClient:
    """Use HTTP in the combined server and an in-process API in component tests."""

    def __init__(
        self,
        target: DeploymentTarget | None = None,
        *,
        base_url: str = "",
        transport: Transport | None = None,
    ) -> None:
        self._in_process = False
        self._target = target
        if transport is not None:
            self._transport = transport
            return
        base_url = base_url or os.environ.get(
            "KUBE_AGENTS_PORTAL_API_URL", ""
        ).strip()
        if base_url:
            if target is None:
                raise ValueError("target is required when using the portal API")
            self._transport = httpx.Client(
                base_url=f"{base_url.rstrip('/')}/",
                headers={
                    **deployment_target_headers(target),
                    **portal_api_headers(),
                },
                timeout=httpx.Timeout(30, connect=5),
            )
            return
        if target is None:
            raise ValueError("target is required when no portal API URL is configured")
        service = ChatService(
            lambda: RuntimeChatBackend(target),
            poll_interval=0.001,
            quiet_polls=2,
            task_timeout=5,
        )
        self._transport = TestClient(
            create_app(
                service,
                runtime_provider_factory=target_runtime_factory(target),
                bound_target=target,
            ),
            base_url="http://testserver/api/v1/",
            headers=portal_api_headers(),
        )
        self._in_process = True

    @staticmethod
    def _payload(response: Response) -> dict:
        payload = response.json()
        if 200 <= response.status_code < 300 and isinstance(payload, dict):
            return payload
        detail = payload.get("detail", payload) if isinstance(payload, dict) else {}
        if isinstance(detail, list):
            issues = []
            for item in detail[:3]:
                if not isinstance(item, dict):
                    continue
                location = ".".join(
                    str(part)
                    for part in item.get("loc", ())
                    if part != "body"
                )
                description = str(item.get("msg") or "").strip()
                if description:
                    issues.append(
                        f"{location}: {description}" if location else description
                    )
            message = "; ".join(issues) or "Portal API request failed."
        else:
            error = detail.get("error", detail) if isinstance(detail, dict) else {}
            message = str(error.get("message") or "Portal API request failed.")
        guidance = (
            "Reconnect the portal or inspect the FastAPI service logs before retrying."
        )
        raise PortalApiError(message, guidance)

    def _get(self, path: str, **kwargs) -> Response:
        try:
            return self._transport.get(path, **kwargs)
        except httpx.TimeoutException as exc:
            raise PortalApiError(
                "Portal API request timed out.",
                "The cluster may be slow or unreachable. Reconnect, then retry.",
            ) from exc
        except httpx.HTTPError as exc:
            raise PortalApiError(
                f"Portal API request failed: {exc}",
                "Reconnect the portal or inspect the FastAPI service logs before retrying.",
            ) from exc

    def _post(self, path: str, **kwargs) -> Response:
        try:
            return self._transport.post(path, **kwargs)
        except httpx.TimeoutException as exc:
            raise PortalApiError(
                "Portal API request timed out.",
                "The cluster may be slow or unreachable. Reconnect, then retry.",
            ) from exc
        except httpx.HTTPError as exc:
            raise PortalApiError(
                f"Portal API request failed: {exc}",
                "Reconnect the portal or inspect the FastAPI service logs before retrying.",
            ) from exc

    def list_agents(self) -> tuple[str, ...]:
        payload = self._payload(self._get("agents"))
        return tuple(str(item) for item in payload.get("agents", []))

    def canonical_agent(self) -> str:
        agents = self.list_agents()
        if len(agents) != 1:
            expected = canonical_platform_agent_name()
            raise PortalApiError(
                "The portal API did not select one canonical PlatformAgent.",
                f"Reconnect the target and confirm PlatformAgent/{expected} exists.",
            )
        return agents[0]

    def inspect_llm_gateway(self) -> dict[str, Any]:
        return self._payload(self._get("llm-gateway", timeout=120))

    def inspect_google_chat_integration(self) -> dict[str, Any]:
        return self._payload(self._get("integrations/google-chat", timeout=120))

    def llm_gateway_device_status(self) -> dict[str, Any]:
        return self._payload(self._get("llm-gateway/device-status", timeout=30))

    def configure_llm_gateway(
        self,
        *,
        provider_id: str,
        model: str,
        credential: str = "",
        settings: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return self._payload(
            self._post(
                "llm-gateway/configuration",
                json={
                    "providerId": provider_id,
                    "model": model,
                    "credential": credential,
                    "settings": settings or {},
                },
                timeout=360,
            )
        )

    def list_conversations(
        self,
        agent: str,
        *,
        cutoff: datetime,
        limit: int = 200,
    ) -> HistoryResult:
        payload = self._payload(
            self._get(
                f"agents/{quote(agent, safe='')}/sessions",
                params={"cutoff": cutoff.isoformat(), "limit": limit},
            )
        )
        conversations = tuple(
            AgentConversation(
                session_id=str(row["session_id"]),
                profile=str(row["profile"]),
                platform=str(row["platform"]),
                user=str(row["user"]),
                attribution=str(row["attribution"]),
                title=str(row["title"]),
                preview=str(row["preview"]),
                chat_type=str(row["chat_type"]),
                chat_id=str(row["chat_id"]),
                thread_id=str(row["thread_id"]),
                started_at=_time(row["started_at"]),
                last_active=_time(row["last_active"]),
                message_count=int(row["message_count"]),
                tool_call_count=int(row["tool_call_count"]),
            )
            for row in payload.get("conversations", [])
        )
        return HistoryResult(conversations, bool(payload.get("truncated")))

    def get_messages(
        self,
        agent: str,
        *,
        profile: str,
        session_id: str,
        limit: int = 500,
    ) -> MessageResult:
        path = (
            f"agents/{quote(agent, safe='')}/sessions/"
            f"{quote(profile, safe='')}/{quote(session_id, safe='')}/messages"
        )
        payload = self._payload(self._get(path, params={"limit": limit}))
        messages = tuple(
            AgentMessage(
                message_id=int(row["message_id"]),
                role=str(row["role"]),
                content=str(row["content"]),
                occurred_at=_time(row["occurred_at"]),
            )
            for row in payload.get("messages", [])
        )
        return MessageResult(messages, bool(payload.get("truncated")))

    def get_task_updates(
        self,
        agent: str,
        *,
        session_id: str,
        limit: int = 100,
    ) -> TaskUpdateResult:
        path = (
            f"agents/{quote(agent, safe='')}/sessions/"
            f"{quote(session_id, safe='')}/tasks"
        )
        payload = self._payload(self._get(path, params={"limit": limit}))
        tasks = tuple(
            AgentTaskUpdate(
                task_id=str(row["task_id"]),
                title=str(row["title"]),
                assignee=str(row["assignee"]),
                status=str(row["status"]),
                created_at=_time(row["created_at"]),
                updated_at=_time(row["updated_at"]),
                summary=str(row["summary"]),
                error=str(row["error"]),
                run_count=int(row.get("run_count", 0)),
                latest_event=str(row.get("latest_event", "")),
                latest_event_at=(
                    _time(row["latest_event_at"])
                    if row.get("latest_event_at")
                    else None
                ),
                previous_error=str(row.get("previous_error", "")),
            )
            for row in payload.get("tasks", [])
        )
        return TaskUpdateResult(tasks, bool(payload.get("truncated")))

    def start_interaction(
        self,
        agent: str,
        *,
        prompt: str,
        session_id: str,
        history: list[dict[str, str]],
        profile: str = "default",
    ) -> InteractionView:
        payload = self._payload(
            self._post(
                "interactions",
                json={
                    "agentId": agent,
                    "profile": profile,
                    "sessionId": session_id,
                    "input": {"text": prompt},
                    "history": list(history)[-MAX_HISTORY_MESSAGES:],
                },
            )
        )
        view = self._interaction(payload)
        if self._in_process:
            deadline = time.monotonic() + 1
            while view.status in {"queued", "running", "waiting_for_tasks"}:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.002)
                view = self.get_interaction(view.interaction_id)
        return view

    def get_interaction(self, interaction_id: str) -> InteractionView:
        payload = self._payload(
            self._get(f"interactions/{quote(interaction_id, safe='')}")
        )
        return self._interaction(payload)

    def resolve_approval(
        self,
        interaction_id: str,
        *,
        choice: str,
    ) -> InteractionView:
        payload = self._payload(
            self._post(
                f"interactions/{quote(interaction_id, safe='')}/approval",
                json={"choice": choice},
            )
        )
        return self._interaction(payload)

    def cancel_interaction(self, interaction_id: str) -> InteractionView:
        payload = self._payload(
            self._post(
                f"interactions/{quote(interaction_id, safe='')}/cancel"
            )
        )
        return self._interaction(payload)

    @staticmethod
    def _interaction(payload: dict) -> InteractionView:
        return InteractionView(
            interaction_id=str(payload["interactionId"]),
            session_id=str(payload["sessionId"]),
            status=str(payload["status"]),
            terminal=bool(payload["terminal"]),
            root_run_id=str(payload.get("rootRunId") or ""),
            output=str(payload.get("output") or ""),
            error=str(payload.get("error") or ""),
            diagnostics=tuple(str(item) for item in payload.get("diagnostics", [])),
            approval=payload.get("approval"),
            tasks=tuple(payload.get("tasks", [])),
            tool_calls=tuple(payload.get("toolCalls", [])),
        )
