"""Shared configuration for live CUJ scenarios."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable

import pytest

from cuj.utils.acceptance_criteria import AcceptanceCriteria
from cuj.utils.evidence import EvidenceLog
from cuj.utils.interaction import InteractionRunner
from cuj.utils.milestones import MilestoneSuite
from cuj.utils.portal import CANONICAL_AGENT_ID, PortalError, isolated_portal

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
        *,
        default_timeout: float = DEFAULT_TIMEOUT_SECONDS,
        default_poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> ScenarioConfig:
        return cls(
            endpoint=endpoint,
            agent_id=CANONICAL_AGENT_ID,
            profile=os.environ.get("CUJ_PROFILE", "default").strip() or "default",
            timeout=float(os.environ.get("CUJ_TIMEOUT", default_timeout)),
            poll_interval=float(
                os.environ.get("CUJ_POLL_INTERVAL", default_poll_interval)
            ),
        )


@dataclass(frozen=True)
class Scenario:
    id: str
    build_prompt: Callable[[], str]
    evaluate_acceptance: Callable[[dict[str, Any]], AcceptanceCriteria]
    evaluate_milestones: Callable[[dict[str, Any]], MilestoneSuite] | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", self.id):
            raise ValueError("scenario id must contain lowercase letters, digits, or _")

    def run_test(self) -> None:
        log = EvidenceLog.temporary(f"kube-agents-{self.id}-")
        try:
            prompt = self.build_prompt()
            with isolated_portal(log.root) as endpoint:
                config = ScenarioConfig.from_env(endpoint)
                interaction = InteractionRunner(config, log).run(
                    prompt,
                    session_prefix=f"portal_{self.id}",
                )
            acceptance = self.evaluate_acceptance(interaction)
            milestones = None
            milestone_results = ()
            milestone_summary = None
            milestone_error = ""
            if self.evaluate_milestones is not None:
                try:
                    milestones = self.evaluate_milestones(interaction)
                    milestone_results = milestones.results
                    milestone_summary = milestones.summary()
                except Exception as exc:  # diagnostic evaluation is non-gating
                    milestones = None
                    milestone_error = f"{type(exc).__name__}: {exc}"
                    log.record("milestone_error", milestone_error)
            for result in milestone_results:
                log.record("milestone", result.to_dict())
            for result in acceptance.results:
                log.record("acceptance_criterion", result.to_dict())
            log.record(
                "summary",
                {
                    "interaction": {
                        "status": interaction.get("status"),
                        "error": interaction.get("error") or None,
                        "diagnostics": interaction.get("diagnostics") or [],
                    },
                    "acceptanceCriteria": acceptance.summary(),
                    "milestones": milestone_summary,
                    "milestoneError": milestone_error or None,
                },
            )
        except (OSError, ValueError, PortalError) as exc:
            pytest.fail(f"{self.id} setup failed: {exc}; evidence: {log.path}")

        status = str(interaction.get("status") or "unknown").upper()
        error = str(interaction.get("error") or "").strip()
        print(f"Interaction outcome: {status}" + (f" — {error}" if error else ""))
        for diagnostic in interaction.get("diagnostics") or []:
            print(f"      Diagnostic: {diagnostic}")
        if milestones is not None:
            print("Milestones (diagnostic only):")
            for line in milestones.report_lines():
                print(line)
        elif milestone_error:
            print(f"Milestones unavailable (diagnostic only): {milestone_error}")
        print("Acceptance criteria (test outcome):")
        for line in acceptance.report_lines():
            print(line)
        print(f"Evidence: {log.path}")
        interaction_failure = (
            f"interaction {status}"
            + (f": {error}" if error else "")
            + "; "
            if interaction.get("status") != "completed"
            else ""
        )
        assert acceptance.passed, (
            interaction_failure
            + "acceptance criteria not met: "
            f"{acceptance.failure_summary()}; evidence: {log.path}"
        )
