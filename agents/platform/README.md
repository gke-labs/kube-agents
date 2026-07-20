# Platform Agent Configs & Scripts

This folder contains the source of truth configuration, schemas, and helper scripts for the SRE Platform Agent.

## Folder Layout

* **`config.yaml`**: The active configuration yaml file for Hermes MCP server parameters, plugin list, and active tools.
* **`scripts/`**: Core python runners and bridges:
  * [`platform_mcp_server.py`](file:///usr/local/google/home/jayantid/kube-agents/agents/platform/scripts/platform_mcp_server.py): Implements SRE diagnostics tools for logs, audit searches, and notification dispatching.
  * [`session_kv_server.py`](file:///usr/local/google/home/jayantid/kube-agents/agents/platform/scripts/session_kv_server.py): FastAPI proxy for mapping GKE event notifications to persistent agent sessions and Slack/Google Chat threads.
  * [`github_token_refresh.py`](file:///usr/local/google/home/jayantid/kube-agents/agents/platform/scripts/github_token_refresh.py): Daemon/job script for minting Google OIDC identity tokens and caching them for git operations.
* **`docs/`**: Integration specs and design guidelines:
  * [**Session Management & Thread Routing**](file:///usr/local/google/home/jayantid/kube-agents/agents/platform/docs/session_management.md): Details the end-to-end event alert thread routing design.
* **`skills/`**: Core agentic skills defining diagnostic and remediation operations for GKE workloads.
