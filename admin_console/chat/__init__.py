"""Shared interaction domain used by the portal API and its clients."""

from admin_console.chat.models import (
    Interaction,
    InteractionEvent,
    InteractionStatus,
    TaskProjection,
    ToolCallEvidence,
)
from admin_console.chat.service import ChatService

__all__ = [
    "ChatService",
    "Interaction",
    "InteractionEvent",
    "InteractionStatus",
    "TaskProjection",
    "ToolCallEvidence",
]
