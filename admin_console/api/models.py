"""Strict HTTP command models for the versioned portal API."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from admin_console.agent_chat import MAX_HISTORY_MESSAGES
from admin_console.chat.models import is_portal_session_id


class InteractionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=32_000)


class HistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=32_000)


class StartInteractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(alias="agentId", pattern=r"^[a-z0-9][a-z0-9.-]{0,62}$")
    profile: str = Field(default="default", pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    session_id: str = Field(default="", alias="sessionId", max_length=256)
    input: InteractionInput
    history: list[HistoryMessage] = Field(
        default_factory=list,
        max_length=MAX_HISTORY_MESSAGES,
    )

    @field_validator("session_id")
    @classmethod
    def portal_session_only(cls, value: str) -> str:
        if value and not is_portal_session_id(value):
            raise ValueError("sessionId must identify a portal-owned session")
        return value


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    choice: str = Field(pattern="^(once|deny)$")
