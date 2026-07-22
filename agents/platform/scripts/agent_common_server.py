#!/usr/bin/env python3
# agent_common_server.py - Shared MCP Server for Inter-Agent Communication and Common Tools.
# Exposes a secure 'call_agent' tool and other shared capabilities to all agents.

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

def load_slack_token():
    """Load SLACK_BOT_TOKEN dynamically from Kubernetes secret if missing from environment."""
    if "SLACK_BOT_TOKEN" not in os.environ:
        try:
            import base64
            import subprocess
            res = subprocess.run(
                ["kubectl", "get", "secret", "platform-agent-secrets", "-n", "kubeagents-system", "-o", "jsonpath={.data.SLACK_BOT_TOKEN}"],
                capture_output=True, text=True, check=True, timeout=10
            )
            val = res.stdout.strip()
            if val:
                os.environ["SLACK_BOT_TOKEN"] = base64.b64decode(val).decode("utf-8")
        except Exception:
            pass

# Run Slack token resolution once at module load
load_slack_token()

def _run_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a subprocess env with HOME redirected to /tmp for GKE container compatibility."""
    return {**os.environ, "HOME": "/tmp", **(extra or {})}



def resolve_agent_credentials(agent_id: str) -> tuple[str, str]:
    """Retrieve the target agent's endpoint and shared API key."""
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
        endpoint = os.environ.get("PLATFORM_API_URL") or "platform-agent.kubeagents-system.svc.cluster.local:8642"
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

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    headers.update(SESSION_MANAGER.delegation_headers(context))

    payload = {
        "model": "hermes-agent",
        "messages": [{"role": "user", "content": query}]
    }

    log(f"Sending secure synchronous call to '{target_agent_id}' at {url}")
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
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
