#!/usr/bin/env python3
# agent_call.py - Generic, synchronous cross-cluster agent client (Secure token-authorized).
# Resolves the target agent's FQDN and secure API key from state, and executes a synchronous HTTP API completions call.

import json
import sys
import os
import urllib.request
import urllib.error
from pathlib import Path

def log(msg: str):
    print(f"[AGENT-CLIENT] {msg}", file=sys.stderr)

def get_hermes_home() -> Path:
    """Return the active HERMES_HOME directory."""
    return Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))

def get_state_file(agent_id: str) -> Path:
    """Return the path to the corresponding agents JSONL state file based on agent type."""
    # Dynamically route to the correct registry file
    if agent_id.startswith("operator-"):
        return get_hermes_home() / "operator_agents.jsonl"
    else:
        # Placeholder for future devteam registry
        return get_hermes_home() / "devteam_agents.jsonl"

def resolve_agent_credentials(agent_id: str) -> tuple[str, str]:
    """Retrieve the target agent's stable K8s Service FQDN and secure API key from the state registry."""
    state_file = get_state_file(agent_id)
    endpoint = ""
    api_key = "none" # Default fallback if unauthenticated

    if state_file.exists():
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    if entry.get("agent_id") == agent_id:
                        endpoint = entry.get("endpoint", "")
                        api_key = entry.get("api_key", "none")
                        log(f"Resolved credentials for '{agent_id}' from state registry.")
                        break
        except Exception as e:
            log(f"Warning: Failed to read state file '{state_file}': {e}")

    if not endpoint:
        # Fallback: If no custom FQDN is registered, use GKE Multi-Cluster Services (MCS) standard FQDN
        endpoint = f"{agent_id}.agent-system.svc.clusterset.local:8642"
        log(f"Info: Using GKE Multi-Cluster Services (MCS) FQDN: {endpoint}")

    return endpoint, api_key

def call_agent_api(endpoint: str, api_key: str, query: str, agent_id: str) -> str:
    """Perform the synchronous HTTP POST call to the target agent's completions API using Bearer Token auth."""
    # Support both secure HTTPS and internal HTTP routes dynamically
    protocol = "https" if endpoint.startswith("https://") else "http"
    clean_endpoint = endpoint.replace("http://", "").replace("https://", "")
    
    url = f"{protocol}://{clean_endpoint}/v1/chat/completions"
    
    headers = {
        "Content-Type": "application/json",
        # Secure Token-Based Bearer Authentication (Dynamic exchange)
        "Authorization": f"Bearer {api_key}"
    }
    payload = {
        "model": "hermes-agent",
        "messages": [{"role": "user", "content": query}]
    }

    log(f"Sending secure synchronous call to '{agent_id}' at: {url}")
    req = urllib.request.Request(
        url, 
        data=json.dumps(payload).encode("utf-8"), 
        headers=headers,
        method="POST"
    )

    try:
        # 5-minute timeout to accommodate complex GKE reasoning and tool executions
        with urllib.request.urlopen(req, timeout=300) as response:
            resp_data = json.loads(response.read().decode("utf-8"))
            return resp_data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        log(f"Error: Target agent returned HTTP {e.code}: {err_body}")
        sys.exit(1)
    except Exception as e:
        log(f"Error: Network communication failed: {e}")
        sys.exit(1)

def main():
    if len(sys.argv) < 3:
        print("Usage: agent_call.py <target_agent_id> <query>", file=sys.stderr)
        sys.exit(1)

    target_agent_id = sys.argv[1]
    query = sys.argv[2]

    # 1. Resolve stable K8s Service FQDN and secure API key
    endpoint, api_key = resolve_agent_credentials(target_agent_id)

    # 2. Execute secure synchronous call and print response directly to stdout
    response_text = call_agent_api(endpoint, api_key, query, target_agent_id)
    print(response_text)

if __name__ == "__main__":
    main()
