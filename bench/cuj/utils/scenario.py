"""Shared configuration for live CUJ scenarios."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable

import pytest

from cuj.utils.evidence import EvidenceLog
from cuj.utils.interaction import InteractionRunner
from cuj.utils.milestones import MilestoneSuite
from cuj.utils.portal import PortalError, isolated_portal

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


@dataclass(frozen=True)
class Scenario:
    id: str
    build_prompt: Callable[[], str]
    evaluate: Callable[[dict[str, Any]], MilestoneSuite]

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", self.id):
            raise ValueError("scenario id must contain lowercase letters, digits, or _")

    def run_test(self) -> None:
        log = EvidenceLog.temporary(f"kube-agents-{self.id}-")
        try:
            prompt = self.build_prompt()
            with isolated_portal(log.root) as endpoint:
                config = ScenarioConfig.from_env(endpoint, self.id)
                interaction = InteractionRunner(config, log).run(
                    prompt,
                    session_prefix=f"portal_{self.id}",
                )
            suite = self.evaluate(interaction)
            for result in suite.results:
                log.record("milestone", result.to_dict())
            log.record("summary", suite.summary())
        except (OSError, ValueError, PortalError) as exc:
            pytest.fail(f"{self.id} setup failed: {exc}; evidence: {log.path}")

        for line in suite.report_lines():
            print(line)
        print(f"Evidence: {log.path}")
        assert suite.passed, (
            f"milestones not met: {suite.failure_summary()}; evidence: {log.path}"
        )
