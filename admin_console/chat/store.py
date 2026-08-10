"""Thread-safe interaction and event storage."""

from __future__ import annotations

import threading
from dataclasses import replace

from admin_console.chat.models import Interaction, InteractionEvent, utc_now


class InteractionStore:
    """In-process store with ordered events and condition-based observation.

    This first delivery is intentionally single-process. The API refuses a
    multi-worker configuration until the store is replaced by a durable shared
    implementation.
    """

    def __init__(self) -> None:
        self._interactions: dict[str, Interaction] = {}
        self._events: dict[str, list[InteractionEvent]] = {}
        self._condition = threading.Condition(threading.RLock())

    def create(self, interaction: Interaction) -> Interaction:
        with self._condition:
            if interaction.interaction_id in self._interactions:
                raise ValueError("interaction already exists")
            self._interactions[interaction.interaction_id] = interaction
            self._events[interaction.interaction_id] = []
            self._condition.notify_all()
            return interaction

    def get(self, interaction_id: str) -> Interaction | None:
        with self._condition:
            return self._interactions.get(interaction_id)

    def update(self, interaction_id: str, **changes) -> Interaction:
        with self._condition:
            current = self._interactions.get(interaction_id)
            if current is None:
                raise KeyError(interaction_id)
            updated = replace(current, updated_at=utc_now(), **changes)
            self._interactions[interaction_id] = updated
            self._condition.notify_all()
            return updated

    def append_event(
        self,
        interaction_id: str,
        event: str,
        data: dict | None = None,
    ) -> InteractionEvent:
        with self._condition:
            if interaction_id not in self._interactions:
                raise KeyError(interaction_id)
            events = self._events[interaction_id]
            item = InteractionEvent(len(events) + 1, event, utc_now(), data or {})
            events.append(item)
            self._condition.notify_all()
            return item

    def events_after(
        self,
        interaction_id: str,
        sequence: int = 0,
    ) -> tuple[InteractionEvent, ...]:
        with self._condition:
            if interaction_id not in self._events:
                raise KeyError(interaction_id)
            return tuple(
                event
                for event in self._events[interaction_id]
                if event.sequence > sequence
            )

    def wait_for_change(
        self,
        interaction_id: str,
        *,
        after_sequence: int,
        timeout: float,
    ) -> None:
        with self._condition:
            self._condition.wait_for(
                lambda: len(self._events.get(interaction_id, ())) > after_sequence
                or (
                    self._interactions.get(interaction_id) is not None
                    and self._interactions[interaction_id].terminal
                ),
                timeout=max(0.0, timeout),
            )
