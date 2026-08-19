"""Shared configuration for live CUJ scenarios."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_TIMEOUT_SECONDS = 1600.0
DEFAULT_POLL_INTERVAL_SECONDS = 2.0


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"required environment variable: {name}")
    return value


@dataclass(frozen=True)
class ScenarioConfig:
    endpoint: str
    agent_id: str
    profile: str = "default"
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS

    def __post_init__(self) -> None:
        if not self.endpoint.strip():
            raise ValueError("scenario endpoint must not be empty")
        if not self.agent_id.strip():
            raise ValueError("scenario agent_id must not be empty")
        if self.timeout <= 0 or self.poll_interval <= 0:
            raise ValueError("scenario timeout and poll_interval must be positive")

    @classmethod
    def from_env(
        cls,
        endpoint: str,
        env_prefix: str,
        *,
        default_timeout: float = DEFAULT_TIMEOUT_SECONDS,
        default_poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> ScenarioConfig:
        prefix = env_prefix.strip().upper()
        if not prefix:
            raise ValueError("scenario environment prefix must not be empty")
        return cls(
            endpoint=endpoint,
            agent_id=required_env(f"{prefix}_AGENT_ID"),
            profile=os.environ.get(f"{prefix}_PROFILE", "default").strip()
            or "default",
            timeout=float(os.environ.get(f"{prefix}_TIMEOUT", default_timeout)),
            poll_interval=float(
                os.environ.get(f"{prefix}_POLL_INTERVAL", default_poll_interval)
            ),
        )
