"""Preflight fixture override for harness CUJs when using the Web Console."""

from __future__ import annotations

from contextlib import ExitStack
import os
from typing import Iterator

import pytest

from cuj.conftest import agent_preflight_results as _portal_preflight_results
from cuj.harness.common import (
    COMPLETED_STATUS,
    WEB_CONSOLE_CLUSTER_MODE_ENV,
    WEB_CONSOLE_URL_ENV,
    _run_web_console_turn,
    managed_web_console_endpoint,
)
from cuj.utils.evidence import EvidenceLog
from cuj.utils.portal import (
    PortalError,
    configured_agent_profiles,
)

PREFLIGHT_PROMPT = "Reply with exactly READY and nothing else. Do not use tools."
EXPECTED_PREFLIGHT_REPLY = "READY"
PREFLIGHT_TEST_BASENAME = "test_00_agent_responsive.py"


@pytest.fixture(scope="session")
def agent_preflight_results() -> Iterator[dict[tuple[str, str], str]]:
    """Verify a managed or configured private Web Console, else ``isolated_portal``."""

    web_console_url = os.environ.get(WEB_CONSOLE_URL_ENV, "").strip()
    cluster_mode = os.environ.get(WEB_CONSOLE_CLUSTER_MODE_ENV, "").strip()
    if not web_console_url and not cluster_mode:
        yield _portal_preflight_results.__wrapped__()
        return

    configured = configured_agent_profiles()
    results: dict[tuple[str, str], str] = {}
    log = EvidenceLog.temporary("kube-agents-webui-prerequisite-")
    with ExitStack() as stack:
        try:
            resolved_url = stack.enter_context(managed_web_console_endpoint())
            interaction = _run_web_console_turn(
                resolved_url,
                PREFLIGHT_PROMPT,
                log,
            )
            status = str(interaction.get("status") or "unknown")
            response = str(interaction.get("output") or "").strip()
            if (
                status != COMPLETED_STATUS
                or response != EXPECTED_PREFLIGHT_REPLY
            ):
                raise PortalError(
                    "web console preflight did not return READY: "
                    f"status={status}, output={response!r}"
                )
        except (OSError, ValueError, PortalError) as exc:
            detail = f"{exc}; evidence: {log.path}"
            for key in configured:
                results[key] = detail
        else:
            for key in configured:
                results[key] = ""
            print(
                f"Web Console prerequisite passed ({resolved_url}); "
                f"latency: {interaction.get('latencySeconds')}s; "
                f"evidence: {log.path}"
            )
        yield results


@pytest.fixture(autouse=True)
def require_responsive_agent_profiles(
    request: pytest.FixtureRequest,
    agent_preflight_results: dict[tuple[str, str], str],
) -> None:
    """Fail the first harness test (and skip the rest) when preflight fails without test_00."""

    if getattr(request.node.fspath, "basename", "") == PREFLIGHT_TEST_BASENAME:
        return
    failures = [msg for msg in agent_preflight_results.values() if msg]
    if not failures:
        return
    session_items = getattr(request.session, "items", None) or ()
    has_preflight_test = any(
        getattr(getattr(item, "fspath", None), "basename", "")
        == PREFLIGHT_TEST_BASENAME
        for item in session_items
    )
    if not has_preflight_test and not getattr(
        request.session, "_harness_preflight_failed_once", False
    ):
        setattr(request.session, "_harness_preflight_failed_once", True)
        pytest.fail(f"agent prerequisite failed: {failures[0]}")
    pytest.skip(f"agent prerequisite failed: {failures[0]}")
