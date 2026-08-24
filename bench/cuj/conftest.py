"""Collection policy for the manually run CUJ suite."""

from __future__ import annotations

import pytest

from cuj.utils.evidence import EvidenceLog
from cuj.utils.portal import (
    PortalError,
    configured_agent_profiles,
    isolated_portal,
    verify_agent,
)


@pytest.fixture(scope="session")
def agent_preflight_results() -> dict[tuple[str, str], str]:
    """Prove every configured agent/profile before any worker runs a CUJ."""

    configured = configured_agent_profiles()
    results: dict[tuple[str, str], str] = {}
    for agent_id, profile in configured:
        log = EvidenceLog.temporary("kube-agents-prerequisite-")
        try:
            with isolated_portal(log.root) as endpoint:
                evidence = verify_agent(
                    endpoint,
                    agent_id,
                    log,
                    profile=profile,
                )
        except (OSError, PortalError) as exc:
            detail = str(exc)
            if "evidence:" not in detail:
                detail += f"; evidence: {log.root}"
            results[(agent_id, profile)] = detail
        else:
            results[(agent_id, profile)] = ""
            print(
                f"Agent prerequisite passed: {agent_id}/{profile}; "
                f"evidence: {evidence}"
            )
    return results


@pytest.fixture(autouse=True)
def require_responsive_agent_profiles(request, agent_preflight_results) -> None:
    """Prevent CUJ bodies from running before this worker's live preflight."""

    if "test_00_agent_responsive.py" in request.node.nodeid:
        return
    failures = [reason for reason in agent_preflight_results.values() if reason]
    if failures:
        pytest.skip(f"agent prerequisite failed: {failures[0]}")


def pytest_collection_modifyitems(items: list) -> None:
    """Run the live-agent prerequisite before scenario tests."""

    items.sort(key=lambda item: "test_00_agent_responsive.py" not in item.nodeid)
