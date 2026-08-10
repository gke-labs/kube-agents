"""FastAPI application for shared portal and evaluator interactions."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from admin_console.api.models import ApprovalRequest, StartInteractionRequest
from admin_console.chat.backend import persisted_backend_factory
from admin_console.chat.service import ChatService


def _error(code: str, message: str, *, retryable: bool = False) -> dict:
    return {"error": {"code": code, "message": message, "retryable": retryable}}


def create_app(service: ChatService | None = None) -> FastAPI:
    account = os.environ.get("KUBE_AGENTS_ADMIN_USER", "").strip()
    service = service or ChatService(persisted_backend_factory(account))
    app = FastAPI(title="kube-agents admin portal", version="1.0.0")
    app.state.chat_service = service

    @app.get("/healthz")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/readyz")
    def ready() -> dict:
        return {"status": "ready"}

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
    def interaction_events(
        interaction_id: str,
        after: int = Query(default=0, ge=0),
        wait_seconds: float = Query(default=30.0, alias="waitSeconds", ge=0, le=60),
    ) -> StreamingResponse:
        interaction = service.get(interaction_id)
        if interaction is None:
            raise HTTPException(
                status_code=404,
                detail=_error("interaction_not_found", "Interaction was not found."),
            )

        def stream() -> Iterator[str]:
            events = service.store.events_after(interaction_id, after)
            if not events and not interaction.terminal and wait_seconds:
                service.store.wait_for_change(
                    interaction_id,
                    after_sequence=after,
                    timeout=wait_seconds,
                )
                events = service.store.events_after(interaction_id, after)
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


app = create_app()
