---
title: Config reference
description: agents/platform/config.yaml annotated.
sidebar:
  order: 1
---

The Platform Agent's runtime wiring is declared in [`agents/platform/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/config.yaml). It tells Hermes which MCP servers to start, which toolsets to expose to which surfaces, and which plugins to load.

The pod's other profiles have their own configs. The Chat Agent's deliberately minimal [`agents/chat/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/config.yaml): a `router` MCP server for specialist discovery, toolsets pinned to `mcp-router` + `kanban` + the `memory` gate on every surface (including the real `google_chat` ingress key), the chat-side plugins (`session_store`, `session_otel_bridge`, `tool_call_audit`, the first-run `bootstrap_onboarding` hook, and `legacy_slash_commands`, which unwraps a typed `/hermes <subcommand>` into the real gateway command — see its [README](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/defaults/plugins/legacy_slash_commands/README.md)), the `multiuser_memory` provider for per-user memory writes (see [`memory`](#memory) below for why it lives on this profile only), and no file or cloud tools. Note that on an operator-deployed pod the repository file is _not_ what runs: the operator renders its own `config.yaml` into the `<agent>-config` ConfigMap and mounts it over `/opt/data/config.yaml`, so the operator's version wins on the default profile and `agents/chat/config.yaml` must be kept in sync with it — see [Operator](/kube-agents/operator/). The Platform Agent's own `config.yaml` has no such caveat: it is image-owned and force-synced from the baked template on every start. The per-cluster Cluster Agents are stamped from the read-only [`agents/cluster/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/cluster/config.yaml) template — see [Cluster Agents](/kube-agents/concepts/cluster-agents/). This page annotates the Platform Agent's file; the other two are self-documenting by design.

## Shape of the file

Every key the file sets, with its comments elided — the file itself is the canonical copy and
carries the rationale for each value:

```yaml
mcp_servers:
  platform_control:
    command: "/opt/hermes/.venv/bin/python3"
    args:
      - "${HERMES_HOME}/scripts/platform_mcp_server.py"
    lazy: true
    connect_timeout: 120
    timeout: 300
    env:
      KUBERNETES_SERVICE_HOST: "${KUBERNETES_SERVICE_HOST}"
      KUBERNETES_SERVICE_PORT: "${KUBERNETES_SERVICE_PORT}"
      HERMES_HOME: "${HERMES_HOME}"
      GOOGLE_CHAT_PROJECT_ID: "${GOOGLE_CHAT_PROJECT_ID}"
      GOOGLE_CHAT_SUBSCRIPTION_NAME: "${GOOGLE_CHAT_SUBSCRIPTION_NAME}"
      API_SERVER_KEY: "${API_SERVER_KEY}"
      SESSION_KV_DB_PATH: "${SESSION_KV_DB_PATH}"
  gke:
    command: "node"
    args:
      - "/opt/mcp-remote/dist/proxy.js"
      - "https://container.googleapis.com/mcp"
    lazy: true
    connect_timeout: 30
    timeout: 60

platform_toolsets:
  cli:
    - hermes-cli
    - mcp-platform_control
    - mcp-developer_knowledge
    - mcp-gke
  api_server:
    - hermes-api-server
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

agent:
  max_turns: 250

tool_loop_guardrails:
  loop_caps:
    max_web_searches: 200

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

- **`platform_control`** — In-pod Python MCP server (`agents/platform/scripts/platform_mcp_server.py`). Handles session state and agent-internal ops (chat ingress lives with the Chat Agent). Env vars are injected from the pod's environment (Kubernetes DNS variables, Hermes home, Chat Pub/Sub config, API server key, session-KV database path).
- **`gke`** — Remote GKE MCP server proxied via `mcp-remote`. All Kubernetes/GKE reads and writes route through this endpoint.

The two servers are timed out differently on purpose. `platform_control` gets `connect_timeout: 120` for cold-start latency and `timeout: 300` for long reasoning chains — it is a local subprocess, so a slow call is a slow call. `gke` gets `connect_timeout: 30` / `timeout: 60` because it is a remote endpoint reached through `mcp-remote`, where a failed call can consume the whole deadline without ever returning; the rationale is recorded in full alongside the block in [`agents/platform/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/config.yaml). Healthy calls to it measure under a second.

A local server's `env:` block is not a filter over the pod's environment but the whole of what that server receives: Hermes strips an MCP child down to a small allowlist, so a variable the server reads and the block does not name arrives unset, and the read silently yields its default. Adding an `os.environ` read to a local MCP server means adding it here too.

### `platform_toolsets`

Toolsets group MCP servers into named bundles for different Hermes surfaces:

- **`cli`** — Exposed to the Hermes CLI (interactive terminal usage inside the pod).
- **`api_server`** — Exposed to the Hermes REST API (Chat integrations, external callers).

Both include the same MCP servers plus their respective Hermes-native tools (`hermes-cli` / `hermes-api-server`). `mcp-developer_knowledge` (a remote proxy to `developerknowledge.googleapis.com/mcp`) is declared in the shared defaults config ([`deploy/shared/defaults/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/deploy/shared/defaults/config.yaml)) and merged in at build time.

Note that the two files' toolset lists are **unioned**, not overridden — the build-time merge combines two lists as `list(dict.fromkeys(a + b))`. Removing an entry from `agents/platform/config.yaml` alone has no effect if the shared defaults still list it.

There is no `mcp-agent_common` entry. That server exposed a `call_agent` A2A tool that could not reach the Platform Agent in this deployment, and it was removed rather than repaired; delegation is kanban-only.

### `toolsets`

A second, top-level gate distinct from `platform_toolsets`: listing `kanban` here exposes the kanban orchestrator tools (`kanban_create`, `kanban_list`, …) to the Platform Agent as a non-worker profile, so it can create and route delegation cards itself. Workers spawned by the dispatcher get the kanban tools automatically via `HERMES_KANBAN_TASK`.

### `agent`

`max_turns` is the per-turn tool-calling iteration budget. Hermes defaults to 90, which the fleet audits outgrow — the cost audit runs ten checks against every cluster and the drift audit nineteen — so this profile raises it to 250. It is set here rather than in the operator's generated root config because both dispatch paths read the profile's `config.yaml`: kanban workers are spawned with `HERMES_HOME` pinned to the profile, and the cron scheduler resolves `agent.max_turns` from `$HERMES_HOME/config.yaml`. Scoping it to this profile leaves the Chat Agent and the Cluster Agents on the default. The comment in the file itself records the runs that motivated the number.

### `tool_loop_guardrails`

`loop_caps.max_web_searches` bounds how many `web_search` calls one turn may make. Hermes defaults to 50 and resets the counter in `reset_for_turn`, which is the right shape for an interactive session — fifty searches in a single turn there is pathological. A kanban worker is not that shape: outside goal mode the dispatcher spawns it with `chat -q`, so the whole card is one turn and the per-turn cap is really a per-card research budget. A genuine research card exhausts it, and the run ends where it was cut off.

Raised to 200 for this profile only, for the same reason as `max_turns` above: both dispatch paths read the profile's `config.yaml`, so the Chat Agent and the Cluster Agents keep the stock 50, which nothing has approached. 200 is a ceiling rather than a target — a card that reaches it is misbehaving and should be stopped, so do not set `0` (unlimited).

The cap was never the whole defect. What made hitting it expensive was the exit taken when it fired: the halt broke out of the agent loop without showing the model the block result, so a worker with 173 successful searches in hand was never told, never got another turn, and exited without closing its card. That path is repaired in [`deploy/docker/patches/kanban_guardrail_exit.py`](https://github.com/gke-labs/kube-agents/blob/main/deploy/docker/patches/kanban_guardrail_exit.py), whose module docstring carries the analysis. Raising the cap alone would only have moved when the failure happened.

### `memory`

Explicitly disabled — the Platform Agent doesn't retain memory across sessions. Every conversation starts fresh.

No memory provider is configured either. The `multiuser_memory` provider scopes its store by the sender's gateway identity, and the Platform Agent is reached through the kanban dispatcher, which spawns workers with no human identity attached — so per-user memory only makes sense on the [Chat Agent](/kube-agents/concepts/chatops/), the profile that actually receives chat ingress. The Chat Agent records each user's durable facts and resolves them into concrete values before delegating, so the Platform Agent gets what it needs inline in the card body.

### `plugins`

Hermes plugins enabled:

- **`hermes_otel`** — OpenTelemetry export.
- **`tool_call_audit`** — writes per-tool-call records for audit and debug.
- **`incident_context`** — injects Kubernetes incident context into known chat threads on reply (`pre_gateway_dispatch` hook). The work happens on the Chat Agent, which enables it too: the pod runs a single gateway and it is homed at that profile, so an incident-thread reply is dispatched there and never here. It stands aside for a message that starts with `/`: `legacy_slash_commands` is on the same hook, and prepending the triage report first would move the command off the front of the line where that plugin's anchored pattern can no longer see it.

The chat-ingress plugins — `session_store` (durable session state) and `session_otel_bridge` (enriches OTel spans with session context, see [Session metadata](/kube-agents/concepts/observability/#session-metadata-plumbing)) — run on the Chat Agent profile, which owns chat ingress. Their sources live in [`agents/chat/defaults/plugins/`](https://github.com/gke-labs/kube-agents/tree/main/agents/chat/defaults/plugins).

## Related files

- [`agents/platform/SOUL.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/SOUL.md) — persona / system prompt.
- [`agents/platform/AGENTS.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/AGENTS.md) — workspace runtime instructions.
- [`agents/platform/cron/jobs.json`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/cron/jobs.json) — cron watchdog definitions. Advanced by `profile-cron-tick` on the Chat Agent's roster, which owns the only ticking gateway. See [Cron jobs reference](/kube-agents/reference/cron-jobs/).
