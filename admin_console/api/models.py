"""Strict HTTP command models for the versioned portal API."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


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
    history: list[HistoryMessage] = Field(default_factory=list, max_length=100)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    choice: str = Field(pattern="^(once|deny)$")
