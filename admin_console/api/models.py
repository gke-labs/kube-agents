"""Strict HTTP command models for the versioned portal API."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from admin_console.agent_chat import MAX_HISTORY_MESSAGES
from admin_console.chat.models import is_portal_session_id
from admin_console.runtime_contract import canonical_platform_agent_name


class InteractionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=32_000)


class HistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=32_000)


class StartInteractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(
        default_factory=canonical_platform_agent_name,
        alias="agentId",
    )
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

    @field_validator("agent_id")
    @classmethod
    def canonical_agent_only(cls, value: str) -> str:
        expected = canonical_platform_agent_name()
        if value != expected:
            raise ValueError(
                f"agentId must be the canonical PlatformAgent "
                f"{expected}"
            )
        return value


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    choice: str = Field(pattern="^(once|deny)$")


class LlmConfigurationRequest(BaseModel):
    """Provider configuration; credential is write-only and never returned."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(alias="providerId", min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=255)
    credential: str = Field(default="", max_length=16_384)
    settings: dict[str, str] = Field(default_factory=dict)
