---
title: Config reference
description: agents/platform/config.yaml annotated.
sidebar:
  order: 1
---

The Platform Agent's runtime wiring is declared in [`agents/platform/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/config.yaml). It tells Hermes which MCP servers the agent can reach, which toolsets to expose to which surfaces, and which plugins to load.

The pod's other profiles have their own configs. The Chat Agent's deliberately minimal [`agents/chat/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/config.yaml): a `router` MCP server for specialist discovery, toolsets pinned to `mcp-router` + `kanban` + the `memory` gate on every surface (including the real `google_chat` ingress key), the chat-side plugins (`session_store`, `session_otel_bridge`, `tool_call_audit`, the first-run `bootstrap_onboarding` hook, `legacy_slash_commands`, which unwraps a typed `/hermes <subcommand>` into the real gateway command — see its [README](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/defaults/plugins/legacy_slash_commands/README.md) — and `agent_roster`, which injects the routable specialists into every turn so delegation needs no `list_agents` roundtrip, see its [README](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/defaults/plugins/agent_roster/README.md)), a memory provider for per-user and shared memory — `multiuser_memory` by default, though the install chooses it and the operator writes the choice in (see [`memory`](#memory) below), and no file or cloud tools. Note that on an operator-deployed pod the repository file is not the whole story: the operator renders a config of its own into the `<agent>-config` ConfigMap and the entrypoint merges it into `/opt/data/config.yaml` at startup, so the two files are unioned and the operator wins any key they disagree on. `agents/chat/config.yaml` must be kept in sync with it — a list entry removed from one and left in the other survives the merge — see [how config reaches each profile](/kube-agents/operator/platformagent-crd/#how-config-reaches-each-profile). The Platform Agent's own `config.yaml` has no such caveat: it is image-owned and force-synced from the baked template on every start — unless the experimental [`platformFrontDoor`](/kube-agents/operator/platformagent-crd/#platformfrontdoor) flag homes the gateway on this profile, which makes the runtime a writer to that file too and puts it on the same fill-only back-fill the `default` profile's config gets — keys the image adds arrive, keys the live file already holds are left as the agent last wrote them. The per-cluster Cluster Agents are stamped from the read-only [`agents/cluster/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/cluster/config.yaml) template — see [Cluster Agents](/kube-agents/concepts/cluster-agents/). This page annotates the Platform Agent's file; the other two are self-documenting by design.

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
      GOOGLE_CLOUD_PROJECT: "${GOOGLE_CLOUD_PROJECT}"
      GCP_PROJECT_ID: "${GCP_PROJECT_ID}"
      GOOGLE_CHAT_HOME_CHANNEL: "${GOOGLE_CHAT_HOME_CHANNEL}"
      SLACK_HOME_CHANNEL: "${SLACK_HOME_CHANNEL}"
      API_SERVER_KEY: "${API_SERVER_KEY}"
      SESSION_KV_API_KEY: "${SESSION_KV_API_KEY}"
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
    - memory
  api_server:
    - hermes-api-server
    - mcp-platform_control
    - mcp-developer_knowledge
    - mcp-gke
    - memory

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
  provider: ""
  read_only: true
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

MCP servers Hermes exposes to the agent. `developer_knowledge` is not listed here — it comes from the shared defaults this file is merged onto at image build.

Every server is `lazy: true`, so none of them is started at boot. Hermes registers a server's tools from the profile's `cache/mcp_schema_cache.json` and spawns the child process on the first call to one of its tools. The agent sees an identical tool list either way; what changes is who pays the connect. It matters because most Platform Agent turns run in a throwaway kanban worker, and connecting every server eagerly cost each of those workers roughly three seconds before the agent could say anything — a cost the long-lived gateway pays once at boot and a worker re-paid every task. (That figure was measured with four servers declared, before `agent_common` was removed; three remain, so the saving is slightly smaller.) The trade is that a server which cannot start now fails on its first tool call rather than at startup.

- **`platform_control`** — In-pod Python MCP server (`agents/platform/scripts/platform_mcp_server.py`). Handles session state and agent-internal ops (chat ingress lives with the Chat Agent). The `env:` block is an allowlist rather than a pass-through: Hermes gives a stdio MCP server a safe baseline (`PATH`, `HOME`, `TMPDIR`, `XDG_*`) plus exactly the keys named there and drops every other pod variable, so anything a tool needs has to be listed. Currently the Kubernetes DNS variables, Hermes home, the Chat Pub/Sub config, the project ids, the Google Chat and Slack home channels, the API server key, and the Session KV bearer token and database path. A home channel is what `send_notification` falls back to when a notification has no thread to reply into — every alert-driven investigation — and it needs `spec.integration.googleChat.homeChannel` set to carry a value; the bearer token is what lets it read `chat_id` and `thread_id` back in the first place, so an absent one costs the thread and the incident report both. See the comments on those keys in the source file.
- **`gke`** — Remote GKE MCP server proxied via `mcp-remote`. All Kubernetes/GKE reads and writes route through this endpoint.

The two servers are timed out differently on purpose. `platform_control` gets `connect_timeout: 120` for cold-start latency — under `lazy` that bounds the first tool call rather than startup — and `timeout: 300` for long reasoning chains; it is a local subprocess, so a slow call is a slow call. `gke` gets `connect_timeout: 30` / `timeout: 60` because it is a remote endpoint reached through `mcp-remote`, where a failed call can consume the whole deadline without ever returning; the rationale is recorded in full alongside the block in [`agents/platform/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/config.yaml). Healthy calls to it measure under a second.

Which is why adding an `os.environ` read to a local MCP server means adding the variable to that block in the same change: a name the block omits arrives unset, and the read silently yields its default rather than failing.

### `platform_toolsets`

Toolsets group MCP servers into named bundles for different Hermes surfaces:

- **`cli`** — Exposed to the Hermes CLI (interactive terminal usage inside the pod).
- **`api_server`** — Exposed to the Hermes REST API (Chat integrations, external callers).

Both include the same MCP servers plus their respective Hermes-native tools (`hermes-cli` / `hermes-api-server`), and both list `memory`. That entry is a gate, not a tool grant: Hermes only injects the memory provider's tools into a profile that names it, and a specialist reached as a kanban worker resolves its toolsets from `cli` while the API-server path uses `api_server`, so it has to appear on both for the two routes to see the same memory. Which tools it actually yields is decided by the provider — see [`memory`](#memory). `mcp-developer_knowledge` (a remote proxy to `developerknowledge.googleapis.com/mcp`) is declared in the shared defaults config ([`deploy/shared/defaults/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/deploy/shared/defaults/config.yaml)) and merged in at build time.

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

`memory_enabled: false` and `user_profile_enabled: false` switch off Hermes' built-in file store (`MEMORY.md` / `USER.md`). They are not what turns memory off here — they gate a different mechanism from the provider below, and leaving them on would hand this profile a writable file store alongside a provider designed to be read-only.

**Personal memory is impossible on this profile and always will be.** It keys off the gateway identity, and the Platform Agent is reached through the kanban dispatcher, which spawns workers with no human attached — so personal memory only makes sense on the [Chat Agent](/kube-agents/concepts/chatops/), the profile that actually receives chat ingress. The provider fails closed on that by itself: with no user identity it recalls `scope:shared` only.

**Shared memory is readable**, because withholding it did not stop this agent needing the corpus — it stopped it getting the corpus from a curated source. `read_only: true` is what keeps that one-way: it removes `memory_retain` from the schemas the model sees, refuses the call if it is made anyway, and turns off automatic capture. The measurement behind both decisions, and the reasoning for the read-only boundary, are in [`docs/designs/memory.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/memory.md).

`provider` is a default, not the decision. The choice belongs to the install — `--memory=` on `install.sh`, carried as `spec.harness.memory.provider` — and the operator overwrites this key from the CR through the profile's overlay, because this file is baked into the image. It writes an empty value for any provider that cannot be made read-only and scoped by tag, which is every provider except the two Hindsight-backed ones; a per-user file provider has no gateway identity to key on here. The value in the file is what a run without the operator gets: `multiuser_memory` on the Chat Agent, matching the CRD default, and empty on the Platform Agent, because that default is a provider a specialist cannot use. The field itself is documented in the [`PlatformAgent` CRD reference](/kube-agents/operator/platformagent-crd/); which providers are selectable, and what each costs to run, is in [`docs/designs/memory.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/memory.md).

### `plugins`

Hermes plugins enabled:

- **`hermes_otel`** — OpenTelemetry export. Its trace backend is baked into the image but rewritten at container start to the endpoint the operator resolved; see [Deploy → Telemetry](/kube-agents/deploy/telemetry/#pointing-at-your-own-collector).
- **`tool_call_audit`** — writes per-tool-call records for audit and debug.
- **`incident_context`** — injects Kubernetes incident context into known chat threads on reply (`pre_gateway_dispatch` hook). The work happens on the Chat Agent, which enables it too: the pod runs a single gateway and it is homed at that profile, so an incident-thread reply is dispatched there and never here. It stands aside for a message that starts with `/`: `legacy_slash_commands` is on the same hook, and prepending the triage report first would move the command off the front of the line where that plugin's anchored pattern can no longer see it.

The chat-ingress plugins — `session_store` (durable session state) and `session_otel_bridge` (enriches OTel spans with session context, see [Session metadata](/kube-agents/concepts/observability/#session-metadata-plumbing)) — run on the Chat Agent profile, which owns chat ingress. Their sources live in [`agents/chat/defaults/plugins/`](https://github.com/gke-labs/kube-agents/tree/main/agents/chat/defaults/plugins).

## Related files

- [`agents/platform/SOUL.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/SOUL.md) — persona / system prompt.
- [`agents/platform/AGENTS.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/AGENTS.md) — workspace runtime instructions.
- [`agents/platform/cron/jobs.json`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/cron/jobs.json) — cron watchdog definitions. Advanced by `profile-cron-tick` on the Chat Agent's roster, which owns the only ticking gateway. See [Cron jobs reference](/kube-agents/reference/cron-jobs/).
