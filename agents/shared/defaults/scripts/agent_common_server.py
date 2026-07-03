#!/usr/bin/env python3
# agent_common_server.py - Shared MCP Server for Inter-Agent Communication and Common Tools.
# Exposes a secure 'call_agent' tool and other shared capabilities to all agents.

import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import Annotated
from pydantic import Field
from mcp.server.fastmcp import FastMCP

# Initialize the FastMCP server
mcp = FastMCP("Agent Common")

def log(msg: str):
    print(f"[COMMON-MCP] {msg}", file=sys.stderr)


def get_hermes_home() -> Path:
    """Return the active HERMES_HOME directory."""
    return Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))


def get_state_file(agent_id: str) -> Path:
    """Return the path to the corresponding agents JSONL state file based on agent type."""
    if agent_id.startswith("operator-"):
        return get_hermes_home() / "operator_agents.jsonl"
    else:
        return get_hermes_home() / "devteam_agents.jsonl"


def resolve_agent_credentials(agent_id: str) -> tuple[str, str]:
    """Retrieve the target agent's endpoint and shared API key."""
    # All agents share the same API key via platform-agent-secrets
    api_key = os.environ.get("API_SERVER_KEY") or "none"

    # 1. Check if it's the platform agent
    if agent_id.lower() == "platform":
        # Subagents have PLATFORM_API_URL, Platform Agent can use local service DNS
        endpoint = os.environ.get("PLATFORM_API_URL") or "platform-agent.agent-system.svc.cluster.local:8642"
        return endpoint, api_key


    raise ValueError(
        f"ERROR [404]: Could not resolve agent '{agent_id}'. "
        "Valid agent IDs must be 'platform'. Operator and DevTeam agents are disabled."
    )


@mcp.tool()
def call_agent(
    target_agent_id: Annotated[
        str,
        Field(
            pattern=r"^(platform)$", # r"^(platform|operator-.*|devteam-.*)$",
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
    if session_id:
        # Sanitize session_id
        clean_session_id = "".join(c for c in str(session_id) if c.isalnum() or c in "-_.").strip()
        if clean_session_id:
            headers["X-Hermes-Session-Id"] = clean_session_id

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
