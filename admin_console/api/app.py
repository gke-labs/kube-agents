"""FastAPI application for shared portal and evaluator interactions."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Callable

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from admin_console import agent_runtime
from admin_console.api.models import ApprovalRequest, StartInteractionRequest
from admin_console.chat.backend import persisted_backend_factory
from admin_console.chat.service import ChatService
from admin_console.chat.store import SQLiteInteractionStore, interaction_state_path
from admin_console.connection_persistence import load_connection
from admin_console.project_config import (
    TARGET_SCOPE_HEADERS,
    DeploymentTarget,
    deployment_target_headers,
)

RuntimeProviderFactory = Callable[[], agent_runtime.AgentRuntimeProvider]


def _error(code: str, message: str, *, retryable: bool = False) -> dict:
    return {"error": {"code": code, "message": message, "retryable": retryable}}


def _persisted_runtime_factory(account: str) -> RuntimeProviderFactory:
    def build() -> agent_runtime.AgentRuntimeProvider:
        connection = load_connection(account)
        if connection is None:
            raise RuntimeError(
                "No verified portal connection is available. Open Connection and connect "
                "to a kube-agents host first."
            )
        return agent_runtime.AgentRuntimeProvider(connection.target)

    return build


def target_runtime_factory(target: DeploymentTarget) -> RuntimeProviderFactory:
    """Build providers lazily so UI tests and callers can substitute adapters."""

    return lambda: agent_runtime.AgentRuntimeProvider(target)


def create_app(
    service: ChatService | None = None,
    *,
    runtime_provider_factory: RuntimeProviderFactory | None = None,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager] | None = None,
) -> FastAPI:
    account = os.environ.get("KUBE_AGENTS_ADMIN_USER", "").strip()
    service = service or ChatService(
        persisted_backend_factory(account),
        store=SQLiteInteractionStore(interaction_state_path()),
    )
    runtime_provider_factory = runtime_provider_factory or _persisted_runtime_factory(
        account
    )
    app = FastAPI(
        title="kube-agents admin portal",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.chat_service = service

    @app.middleware("http")
    async def reject_stale_target(request: Request, call_next):
        if request.url.path.startswith("/api/v1/"):
            supplied = {
                header: request.headers.get(header, "")
                for header, _ in TARGET_SCOPE_HEADERS
            }
            if any(supplied.values()):
                if not all(supplied.values()):
                    return JSONResponse(
                        status_code=400,
                        content={
                            "detail": _error(
                                "incomplete_target_scope",
                                "The portal target scope is incomplete.",
                            )
                        },
                    )
                connection = load_connection(account)
                if connection is None:
                    return JSONResponse(
                        status_code=503,
                        content={
                            "detail": _error(
                                "connection_unavailable",
                                "No verified portal connection is available.",
                                retryable=True,
                            )
                        },
                    )
                expected = deployment_target_headers(connection.target)
                if supplied != expected:
                    return JSONResponse(
                        status_code=409,
                        content={
                            "detail": _error(
                                "stale_target_scope",
                                "The connected target changed in another portal tab. "
                                "Refresh before continuing.",
                            )
                        },
                    )
        return await call_next(request)

    @app.get("/healthz")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/readyz")
    def ready() -> dict:
        return {"status": "ready"}

    @app.get("/api/v1/agents")
    def list_agents() -> dict:
        try:
            return {"agents": list(runtime_provider_factory().list_agents())}
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=_error("runtime_unavailable", str(exc), retryable=True),
            ) from exc

    @app.get("/api/v1/agents/{agent_id}/sessions")
    def list_sessions(
        agent_id: str,
        cutoff: datetime,
        limit: int = Query(default=200, ge=1, le=200),
    ) -> dict:
        try:
            result = runtime_provider_factory().list_conversations(
                agent_id,
                cutoff=cutoff,
                limit=limit,
            )
            return {
                "conversations": list(result.conversations),
                "truncated": result.truncated,
            }
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=_error("runtime_unavailable", str(exc), retryable=True),
            ) from exc

    @app.get(
        "/api/v1/agents/{agent_id}/sessions/{profile}/{session_id}/messages"
    )
    def get_messages(
        agent_id: str,
        profile: str,
        session_id: str,
        limit: int = Query(default=500, ge=1, le=500),
    ) -> dict:
        try:
            result = runtime_provider_factory().get_messages(
                agent_id,
                profile=profile,
                session_id=session_id,
                limit=limit,
            )
            return {
                "messages": list(result.messages),
                "truncated": result.truncated,
            }
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=_error("runtime_unavailable", str(exc), retryable=True),
            ) from exc

    @app.get("/api/v1/agents/{agent_id}/sessions/{session_id}/tasks")
    def get_tasks(
        agent_id: str,
        session_id: str,
        limit: int = Query(default=100, ge=1, le=200),
    ) -> dict:
        try:
            result = runtime_provider_factory().get_task_updates(
                agent_id,
                session_id=session_id,
                limit=limit,
            )
            return {
                "tasks": list(result.tasks),
                "truncated": result.truncated,
            }
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=_error("runtime_unavailable", str(exc), retryable=True),
            ) from exc

    @app.post(
        "/api/v1/interactions",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_interaction(payload: StartInteractionRequest) -> dict:
        interaction = service.start(
            agent_id=payload.agent_id,
            profile=payload.profile,
            session_id=payload.session_id,
            input_text=payload.input.text,
            history=[message.model_dump() for message in payload.history],
            user_email=account,
        )
        return interaction.to_dict()

    @app.get("/api/v1/interactions/{interaction_id}")
    def get_interaction(interaction_id: str) -> dict:
        interaction = service.get(interaction_id)
        if interaction is None:
            raise HTTPException(
                status_code=404,
                detail=_error("interaction_not_found", "Interaction was not found."),
            )
        return interaction.to_dict()

    @app.get("/api/v1/interactions/{interaction_id}/events")
    async def interaction_events(
        interaction_id: str,
        after: int = Query(default=0, ge=0),
        wait_seconds: float = Query(default=30.0, alias="waitSeconds", ge=0, le=60),
    ) -> StreamingResponse:
        interaction = await asyncio.to_thread(service.get, interaction_id)
        if interaction is None:
            raise HTTPException(
                status_code=404,
                detail=_error("interaction_not_found", "Interaction was not found."),
            )

        async def stream() -> AsyncIterator[str]:
            events = await asyncio.to_thread(
                service.store.events_after,
                interaction_id,
                after,
            )
            if not events and not interaction.terminal and wait_seconds:
                deadline = asyncio.get_running_loop().time() + wait_seconds
                while not events:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(0.25, remaining))
                    events = await asyncio.to_thread(
                        service.store.events_after,
                        interaction_id,
                        after,
                    )
                    if events:
                        break
                    current = await asyncio.to_thread(service.get, interaction_id)
                    if current is None or current.terminal:
                        break
            for event in events:
                yield f"id: {event.sequence}\n"
                yield f"event: {event.event}\n"
                yield f"data: {json.dumps(event.to_dict(), separators=(',', ':'))}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/api/v1/interactions/{interaction_id}/approval")
    def approve_interaction(
        interaction_id: str,
        payload: ApprovalRequest,
    ) -> dict:
        try:
            return service.approve(interaction_id, payload.choice).to_dict()
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail=_error("interaction_not_found", "Interaction was not found."),
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail=_error("invalid_interaction_state", str(exc)),
            ) from exc

    @app.post("/api/v1/interactions/{interaction_id}/cancel")
    def cancel_interaction(interaction_id: str) -> dict:
        try:
            return service.cancel(interaction_id).to_dict()
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail=_error("interaction_not_found", "Interaction was not found."),
            ) from exc

    return app
