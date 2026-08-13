#!/usr/bin/env python3
# agent_common_server.py - Shared MCP Server for Inter-Agent Communication and Common Tools.
# Exposes a secure 'call_agent' tool and other shared capabilities to all agents.

import hashlib
import json
import os
import sys
import urllib.request
import urllib.error

from typing import Annotated
from pydantic import Field
from mcp.server.fastmcp import FastMCP
from session_manager import SessionManager

# Initialize the FastMCP server
mcp = FastMCP("Agent Common")

def log(msg: str):
    print(f"[COMMON-MCP] {msg}", file=sys.stderr)


SESSION_MANAGER = SessionManager()

# Shared Configuration Defaults
CONFIG_PATH = os.environ.get("PLATFORM_AGENT_CONFIG_PATH", "/opt/data/config.yaml")
DOTENV_PATH = os.environ.get("PLATFORM_AGENT_DOTENV_PATH", "/opt/data/.env")

# This module must not spawn a subprocess at import. platform_mcp_server.py
# imports it, so module scope runs twice per platform-profile kanban worker
# spawn — once in the agent_common MCP child (deploy/shared/defaults/config.yaml)
# and once in platform_control (agents/platform/config.yaml) — and once more per
# pod in session_kv_server, which the entrypoint backgrounds as uvicorn
# (deploy/shared/docker-entrypoint.sh step 5) rather than starting per worker.
# Spawning from here therefore buys an extra sandboxed interpreter start on
# every worker, which under gVisor is the dominant per-process cost.
#
# A former load_slack_token() shelled out to `kubectl get secret
# platform-agent-secrets` here. It could not populate SLACK_BOT_TOKEN on either
# path, and the two paths fail for different reasons:
#   - In the MCP children, _build_safe_env withholds CREDENTIAL_PROXY_URL, so
#     the PATH kubectl shim exits 1 (credential_proxy_client.py).
#   - In session_kv_server, which inherits the full pod env, the shim does reach
#     the credential-proxy sidecar and is rejected there: the process cwd is
#     /opt/hermes, outside CREDENTIAL_PROXY_WORKSPACE_ROOT=/opt/data (see
#     _within_workspace in credential_proxy.py, and docker-entrypoint.sh step
#     5.5, which already records the cwd).
# Both readers of the variable (platform_mcp_server.get_active_platform and
# session_kv_server's active-platform helper) consult CONFIG_PATH first and only
# fall back to the env var. Pinned by TestNoSubprocessAtImport in
# test_agent_common_server.py.

def _run_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a subprocess env with HOME redirected to /tmp for GKE container compatibility."""
    return {**os.environ, "HOME": "/tmp", **(extra or {})}



def resolve_agent_credentials(agent_id: str) -> tuple[str, str]:
    """Retrieve the target agent's endpoint and primary API key."""
    api_key = os.environ.get("API_SERVER_KEY", "").strip()
    if not api_key:
        # Fail closed: never fall back to a guessable literal (e.g. "none").
        # A missing secret means the deployment is misconfigured; refuse to
        # send an inter-agent request that would authenticate as a known value.
        raise ValueError(
            "ERROR [500]: API_SERVER_KEY is not configured; refusing to send an "
            "unauthenticated inter-agent request."
        )

    if agent_id.lower() == "platform":
        endpoint = (
            os.environ.get("PLATFORM_API_URL")
            or "https://platform-agent.kubeagents-system.svc.cluster.local:8642"
        )
        return endpoint, api_key

    raise ValueError(f"ERROR [404]: Could not resolve agent '{agent_id}'. Only 'platform' agent is supported.")


@mcp.tool()
def call_agent(
    target_agent_id: Annotated[
        str,
        Field(
            pattern=r"^(platform)$",
            description="The unique ID of the target agent (only 'platform' is a valid target)."
        )
    ],
    query: Annotated[
        str,
        Field(description="The natural language query or operational instruction to send to the target agent.")
    ],
    session_id: Annotated[
        str,
        Field(
            description="Optional. An arbitrary stable string (like a UUID) to maintain conversation "
            "continuity. If you wish to have a continuous, multi-turn conversation with the "
            "target agent, generate a session ID and pass the same value in subsequent calls "
            "to this agent. If omitted, the call is treated as stateless."
        )
    ] = "",
) -> str:
    """
    Directly and securely execute a synchronous, token-authorized completions API call
    to the Platform Agent across the fleet (only 'platform' is a valid target).
    """
    context = SESSION_MANAGER.current_context(session_id)

    try:
        endpoint, api_key = resolve_agent_credentials(target_agent_id)
    except Exception as e:
        return str(e)

    # Robust endpoint cleaning: extract protocol, hostname:port, and ensure clean /v1/chat/completions suffix
    protocol = "https" if endpoint.startswith("https://") else "http"

    # Strip protocol and any trailing path suffixes
    clean_host = endpoint.replace("http://", "").replace("https://", "").split("/")[0]

    url = f"{protocol}://{clean_host}/v1/chat/completions"

    payload = {
        # The name the target's API server advertises on /v1/models, which the
        # operator pins to the LiteLLM model via API_SERVER_MODEL_NAME. Matching
        # it means "use the profile's configured default"; any other string is a
        # real per-request model once `direct_model_requests` is on, and LiteLLM
        # serves nothing else.
        "model": "model-default",
        "messages": [{"role": "user", "content": query}]
    }
    payload_bytes = json.dumps(payload).encode("utf-8")
    body_digest = hashlib.sha256(payload_bytes).hexdigest()

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    headers.update(
        SESSION_MANAGER.signed_delegation_headers(
            context, api_key, body_digest=body_digest, target=target_agent_id
        )
    )

    log(f"Sending secure synchronous call to '{target_agent_id}' at {url}")
    req = urllib.request.Request(
        url,
        data=payload_bytes,
        headers=headers,
        method="POST"
    )

    try:
        # 5-minute timeout to accommodate complex reasoning loops
        with urllib.request.urlopen(req, timeout=300) as response:
            resp_data = json.loads(response.read().decode("utf-8"))
            return resp_data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        return f"ERROR: Target agent returned HTTP {e.code}: {err_body}"
    except Exception as e:
        return f"ERROR: Communication failed: {e}"

if __name__ == "__main__":
    mcp.run()
