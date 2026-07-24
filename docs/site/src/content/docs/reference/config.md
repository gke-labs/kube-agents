---
title: Config reference
description: agents/platform/config.yaml annotated.
sidebar:
  order: 1
---

The Platform Agent's runtime wiring is declared in [`agents/platform/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/config.yaml). It tells Hermes which MCP servers to start, which toolsets to expose to which surfaces, and which plugins to load.

## Full file

```yaml
# MCP Servers configuration.
mcp_servers:
  platform_control:
    command: "/opt/hermes/.venv/bin/python3"
    args:
      - "/opt/data/scripts/platform_mcp_server.py"
    connect_timeout: 120
    # 5-minute timeout to support long GKE reasoning chains
    timeout: 300
    env:
      KUBERNETES_SERVICE_HOST: "${KUBERNETES_SERVICE_HOST}"
      KUBERNETES_SERVICE_PORT: "${KUBERNETES_SERVICE_PORT}"
      HERMES_HOME: "${HERMES_HOME}"
      GOOGLE_CHAT_PROJECT_ID: "${GOOGLE_CHAT_PROJECT_ID}"
      GOOGLE_CHAT_SUBSCRIPTION_NAME: "${GOOGLE_CHAT_SUBSCRIPTION_NAME}"
      API_SERVER_KEY: "${API_SERVER_KEY}"
  gke:
    command: "node"
    args:
      - "/opt/mcp-remote/dist/proxy.js"
      - "https://container.googleapis.com/mcp"

platform_toolsets:
  cli:
    - hermes-cli
    - mcp-agent_common
    - mcp-platform_control
    - mcp-developer_knowledge
    - mcp-gke
  api_server:
    - hermes-api-server
    - mcp-agent_common
    - mcp-platform_control
    - mcp-developer_knowledge
    - mcp-gke

memory:
  memory_enabled: false
  user_profile_enabled: false
  provider: multiuser_memory

plugins:
  enabled:
    - hermes_otel
    - session_store
    - session_otel_bridge
    - tool_call_audit
    - incident_context
```

## Sections

### `mcp_servers`

MCP servers Hermes starts and connects to.

- **`platform_control`** — In-pod Python MCP server (`agents/platform/scripts/platform_mcp_server.py`). Handles chat message routing, session state, and agent-internal ops. Env vars are injected from the pod's environment (Kubernetes DNS variables, Hermes home, Chat Pub/Sub config, API server key).
- **`gke`** — Remote GKE MCP server proxied via `mcp-remote`. All Kubernetes/GKE reads and writes route through this endpoint.

`connect_timeout: 120` allows for cold-start latency; `timeout: 300` accommodates long reasoning chains.

### `platform_toolsets`

Toolsets group MCP servers into named bundles for different Hermes surfaces:

- **`cli`** — Exposed to the Hermes CLI (interactive terminal usage inside the pod).
- **`api_server`** — Exposed to the Hermes REST API (Chat integrations, external callers).

Both include the same MCP servers plus their respective Hermes-native tools (`hermes-cli` / `hermes-api-server`). `mcp-agent_common` (a local Python server, `agent_common_server.py`) and `mcp-developer_knowledge` (a remote proxy to `developerknowledge.googleapis.com/mcp`) are declared in the shared defaults config ([`deploy/shared/defaults/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/deploy/shared/defaults/config.yaml)) and merged in at runtime.

### `memory`

Explicitly disabled — the Platform Agent doesn't retain per-user memory across sessions. Every conversation starts fresh. The `multiuser_memory` provider name is set for future use.

### `plugins`

Hermes plugins enabled:

- **`hermes_otel`** — OpenTelemetry export.
- **`session_store`** — durable session state (writes to the pod's persistent volume if configured).
- **`session_otel_bridge`** — enriches OTel spans with session context (see [Session metadata](/kube-agents/concepts/observability/#session-metadata-plumbing)).
- **`tool_call_audit`** — writes per-tool-call records for audit and debug.
- **`incident_context`** — injects Kubernetes incident context into known chat threads on reply (`pre_gateway_dispatch` hook).

## Related files

- [`agents/platform/SOUL.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/SOUL.md) — persona / system prompt.
- [`agents/platform/AGENTS.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/AGENTS.md) — workspace runtime instructions.
- [`agents/platform/cron/jobs.json`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/cron/jobs.json) — cron watchdog definitions. See [Cron jobs reference](/kube-agents/reference/cron-jobs/).
