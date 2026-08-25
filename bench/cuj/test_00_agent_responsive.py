"""Live prerequisite for every manually run CUJ scenario."""

from __future__ import annotations

import pytest

from cuj.utils.portal import configured_agent_profiles

AGENT_PROFILES = configured_agent_profiles()


@pytest.mark.parametrize(
    ("agent_id", "profile"),
    AGENT_PROFILES,
    ids=[f"{agent_id}:{profile}" for agent_id, profile in AGENT_PROFILES],
)
def test_00_agent_live_and_responsive(
    agent_id: str,
    profile: str,
    agent_preflight_results: dict[tuple[str, str], str],
) -> None:
    failure = agent_preflight_results.get((agent_id, profile), "not checked")
    if failure:
        pytest.fail(f"agent prerequisite failed: {failure}")
