"""Stage 2 E2E Promotion Test: In-Cluster Platform Agent REST API Health Probe."""

import urllib.error
import urllib.request
import json
from typing import Optional

import pytest


def test_platform_agent_api_health_ping(
    port_forward_agent: Optional[str],
    platform_agent_api_key: Optional[str],
) -> None:
    """Verifies that the deployed Platform Agent REST API is reachable, authenticated, and responsive."""
    # No guard on the fixtures here on purpose. port_forward_agent fails during
    # setup, naming the transport it used and what the far end said, so a guard
    # in the test body could only restate that less precisely — the message this
    # one used to carry named a port-forward the suite no longer uses.
    url = f"{port_forward_agent}/v1/responses"
    payload = json.dumps({
        "model": "model-default",
        "conversation": "e2e-health-check-ping",
        "input": "ping",
    }).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {platform_agent_api_key}",
        "Content-Type": "application/json",
    }

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            assert resp.status == 200, f"Expected HTTP 200 from agent API, got {resp.status}"
            body = json.loads(resp.read().decode("utf-8"))
            assert "output" in body or "choices" in body or "response" in body or "assistant" in str(body), (
                f"Agent response missing expected structure: {body}"
            )
    except urllib.error.HTTPError as e:
        error_detail = e.read().decode("utf-8", errors="replace")
        pytest.fail(f"Platform Agent API health ping failed with HTTP {e.code}: {error_detail}")
    except Exception as e:
        pytest.fail(f"Failed to connect to Platform Agent API on {url}: {e}")
