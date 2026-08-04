---
title: Config reference
description: agents/platform/config.yaml annotated.
sidebar:
  order: 1
---

The Platform Agent's runtime wiring is declared in [`agents/platform/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/config.yaml). It tells Hermes which MCP servers to start, which toolsets to expose to which surfaces, and which plugins to load.

The pod's other profiles have their own configs. The Chat Agent's deliberately minimal [`agents/chat/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/config.yaml): a `router` MCP server for specialist discovery, toolsets pinned to `mcp-router` + `kanban` + the `memory` gate on every surface (including the real `google_chat` ingress key), the chat-side plugins (`session_store`, `session_otel_bridge`, `tool_call_audit`, and the first-run `bootstrap_onboarding` hook), the `multiuser_memory` provider for per-user memory writes (see [`memory`](#memory) below for why it lives on this profile only), and no file or cloud tools. Note that on an operator-deployed pod the repository file is _not_ what runs: the operator renders its own `config.yaml` into the `<agent>-config` ConfigMap and mounts it over `/opt/data/config.yaml`, so the operator's version wins on the default profile and `agents/chat/config.yaml` must be kept in sync with it — see [Operator](/kube-agents/operator/). The Platform Agent's own `config.yaml` has no such caveat: it is image-owned and force-synced from the baked template on every start. The per-cluster Cluster Agents are stamped from the read-only [`agents/cluster/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/cluster/config.yaml) template — see [Cluster Agents](/kube-agents/concepts/cluster-agents/). This page annotates the Platform Agent's file; the other two are self-documenting by design.

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

# Top-level `toolsets` gates the kanban orchestrator surface: the kanban tools
# live in the core pool (surfaced via hermes-cli/hermes-api-server), and their
# check_fn requires "kanban" here for a non-worker (orchestrator) profile. This
# lets the Platform Agent create/route kanban cards for delegation. (Workers get
# the kanban tools automatically via HERMES_KANBAN_TASK.) It does not restrict
# any other tools.
toolsets:
  - kanban

memory:
  memory_enabled: false
  user_profile_enabled: false

# The Platform Agent is no longer the chat ingress (the Chat Agent / `default`
# profile owns that), so the session_store / session_otel_bridge ingress plugins
# move to the Chat Agent. Keep otel for observability parity and tool_call_audit
# to audit this privileged specialist's tool calls.
plugins:
  enabled:
    - hermes_otel
    - tool_call_audit
    - incident_context
```

## Sections

### `mcp_servers`

MCP servers Hermes starts and connects to.

- **`platform_control`** — In-pod Python MCP server (`agents/platform/scripts/platform_mcp_server.py`). Handles session state and agent-internal ops (chat ingress lives with the Chat Agent). Env vars are injected from the pod's environment (Kubernetes DNS variables, Hermes home, Chat Pub/Sub config, API server key).
- **`gke`** — Remote GKE MCP server proxied via `mcp-remote`. All Kubernetes/GKE reads and writes route through this endpoint.

`connect_timeout: 120` allows for cold-start latency; `timeout: 300` accommodates long reasoning chains.

### `platform_toolsets`

Toolsets group MCP servers into named bundles for different Hermes surfaces:

- **`cli`** — Exposed to the Hermes CLI (interactive terminal usage inside the pod).
- **`api_server`** — Exposed to the Hermes REST API (Chat integrations, external callers).

Both include the same MCP servers plus their respective Hermes-native tools (`hermes-cli` / `hermes-api-server`). `mcp-agent_common` (a local Python server, `agent_common_server.py`) and `mcp-developer_knowledge` (a remote proxy to `developerknowledge.googleapis.com/mcp`) are declared in the shared defaults config ([`deploy/shared/defaults/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/deploy/shared/defaults/config.yaml)) and merged in at runtime.

### `toolsets`

A second, top-level gate distinct from `platform_toolsets`: listing `kanban` here exposes the kanban orchestrator tools (`kanban_create`, `kanban_list`, …) to the Platform Agent as a non-worker profile, so it can create and route delegation cards itself. Workers spawned by the dispatcher get the kanban tools automatically via `HERMES_KANBAN_TASK`.

### `memory`

Explicitly disabled — the Platform Agent doesn't retain memory across sessions. Every conversation starts fresh.

No memory provider is configured either. The `multiuser_memory` provider scopes its store by the sender's gateway identity, and the Platform Agent is reached through the kanban dispatcher, which spawns workers with no human identity attached — so per-user memory only makes sense on the [Chat Agent](/kube-agents/concepts/chatops/), the profile that actually receives chat ingress. The Chat Agent records each user's durable facts and resolves them into concrete values before delegating, so the Platform Agent gets what it needs inline in the card body.

### `plugins`

Hermes plugins enabled:

- **`hermes_otel`** — OpenTelemetry export.
- **`tool_call_audit`** — writes per-tool-call records for audit and debug.
- **`incident_context`** — injects Kubernetes incident context into known chat threads on reply (`pre_gateway_dispatch` hook).

The chat-ingress plugins — `session_store` (durable session state) and `session_otel_bridge` (enriches OTel spans with session context, see [Session metadata](/kube-agents/concepts/observability/#session-metadata-plumbing)) — run on the Chat Agent profile, which owns chat ingress. Their sources live in [`agents/chat/defaults/plugins/`](https://github.com/gke-labs/kube-agents/tree/main/agents/chat/defaults/plugins).

## Related files

- [`agents/platform/SOUL.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/SOUL.md) — persona / system prompt.
- [`agents/platform/AGENTS.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/AGENTS.md) — workspace runtime instructions.
- [`agents/platform/cron/jobs.json`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/cron/jobs.json) — cron watchdog definitions. See [Cron jobs reference](/kube-agents/reference/cron-jobs/).
