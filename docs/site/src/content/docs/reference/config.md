---
title: Config reference
description: agents/platform/config.yaml annotated.
sidebar:
  order: 1
---

The Platform Agent's runtime wiring is declared in [`agents/platform/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/config.yaml). It tells Hermes which MCP servers the agent can reach, which toolsets to expose to which surfaces, and which plugins to load.

The pod's other profiles have their own configs. The Planning Agent's deliberately minimal [`agents/chat/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/config.yaml): a `router` MCP server for specialist discovery, toolsets pinned to `mcp-router` + `kanban` + the `memory` gate on every surface (including the real `google_chat` ingress key), the chat-side plugins (`session_store`, `session_otel_bridge`, `tool_call_audit`, `chat_metrics`, which counts inbound messages and failed turns for `status.usage`, the first-run `bootstrap_onboarding` hook, `legacy_slash_commands`, which unwraps a typed `/hermes <subcommand>` into the real gateway command and disables `/undo` — see its [README](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/defaults/plugins/legacy_slash_commands/README.md) — and `agent_roster`, which injects the routable specialists into every turn so delegation needs no `list_agents` roundtrip, see its [README](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/defaults/plugins/agent_roster/README.md)), a memory provider for per-user and shared memory — `multiuser_memory` by default, though the install chooses it and the operator writes the choice in (see [`memory`](#memory) below), and no file or cloud tools. Note that on an operator-deployed pod the repository file is not the whole story: the operator renders a config of its own into the `<agent>-config` ConfigMap and the entrypoint merges it into `/opt/data/config.yaml` at startup, so the two files are unioned and the operator wins any key they disagree on. `agents/chat/config.yaml` must be kept in sync with it — a list entry removed from one and left in the other survives the merge — see [how config reaches each profile](/kube-agents/operator/platformagent-crd/#how-config-reaches-each-profile). The Platform Agent's own `config.yaml` has no such caveat: it is image-owned and force-synced from the baked template on every start — unless the experimental [`platformFrontDoor`](/kube-agents/operator/platformagent-crd/#platformfrontdoor) flag homes the gateway on this profile, which makes the runtime a writer to that file too and puts it on the same fill-only back-fill the `default` profile's config gets — keys the image adds arrive, keys the live file already holds are left as the agent last wrote them, with two exceptions that follow the image: the remote MCP `User-Agent` header, and the `plugins.enabled` list, which gains the names the image's list has and the live list lacks ([how config reaches each profile](/kube-agents/operator/platformagent-crd/#how-config-reaches-each-profile)) (see [`mcp_servers`](#mcp_servers) below). The per-cluster Cluster Agents are stamped from the read-only [`agents/cluster/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/cluster/config.yaml) template — see [Cluster Agents](/kube-agents/concepts/cluster-agents/) — and a cluster profile already on the volume is back-filled from it on the same fill-only terms (see [how config reaches each profile](/kube-agents/operator/platformagent-crd/#how-config-reaches-each-profile)). This page annotates the Platform Agent's file; the other two are self-documenting by design.

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
      A2A_NOTIFY_PLATFORM: "${A2A_NOTIFY_PLATFORM}"
      NATS_URL: "${NATS_URL}"
      NATS_CA_FILE: "${NATS_CA_FILE}"
      A2A_BUS_USER: "${A2A_BUS_USER}"
      API_SERVER_KEY: "${API_SERVER_KEY}"
      SESSION_KV_API_KEY: "${SESSION_KV_API_KEY}"
      SESSION_KV_DB_PATH: "${SESSION_KV_DB_PATH}"
      PLATFORM_AGENT_HOME: "${PLATFORM_AGENT_HOME}"
  gke:
    command: "node"
    args:
      - "/opt/mcp-remote/dist/proxy.js"
      - "--header"
      - "User-Agent: kube-agents/${KUBE_AGENTS_VERSION} (daemon)"
      - "${KUBE_AGENTS_MCP_RELAY_URL}/gke"
      - "--allow-http"
      - "--transport"
      - "http-only"
    env:
      MCP_REMOTE_BEARER_FILE: "${CREDENTIAL_PROXY_TOKEN_FILE}"
    lazy: true
    connect_timeout: 30
    timeout: 60
    tools:
      include:
        - check_k8s_auth
        - describe_k8s_resource
        - get_cluster
        - get_k8s_cluster_info
        - get_k8s_logs
        - get_k8s_resource
        - get_k8s_rollout_status
        - get_k8s_version
        - get_node_pool
        - get_operation
        - list_clusters
        - list_k8s_api_resources
        - list_k8s_events
        - list_node_pools
        - list_operations

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
  max_turns: 150
  disabled_toolsets:
    - browser
    - computer_use
    - image_gen
    - tts
    - vision

context_file_max_chars: 100000

tool_loop_guardrails:
  loop_caps:
    max_web_searches: 200
  permission_refusal_halt_after: 1

memory:
  memory_enabled: false
  provider: ""
  read_only: true
  user_profile_enabled: false

# The Platform Agent is not the chat ingress (the Chat Agent / `default`
# profile owns that), so the session_store / session_otel_bridge ingress plugins
# move to the Chat Agent. Keep otel for observability parity and tool_call_audit
# to audit this privileged specialist's tool calls. pr_opened records each pull
# request this profile's kanban workers open on their card, which is what posts
# the thread's PR message (plugins/pr_opened/__init__.py).
plugins:
  enabled:
    - hermes_otel
    - tool_call_audit
    - incident_context
    - pr_opened
```

## Sections

### `mcp_servers`

MCP servers Hermes exposes to the agent. `developer_knowledge` is not listed here — it comes from the shared defaults this file is merged onto at image build.

Every server is `lazy: true`, so none of them is started at boot. Hermes registers a server's tools from the profile's `cache/mcp_schema_cache.json` and spawns the child process on the first call to one of its tools. The agent sees an identical tool list either way; what changes is who pays the connect. It matters because most Platform Agent turns run in a throwaway kanban worker, and connecting every server eagerly cost each of those workers roughly three seconds before the agent could say anything — a cost the long-lived gateway pays once at boot and a worker re-paid every task. The trade is that a server which cannot start now fails on its first tool call rather than at startup.

- **`platform_control`** — In-pod Python MCP server (`agents/platform/scripts/platform_mcp_server.py`). Handles session state and agent-internal ops (chat ingress lives with the Planning Agent). The `env:` block is an allowlist rather than a pass-through: Hermes gives a stdio MCP server a safe baseline (`PATH`, `HOME`, `TMPDIR`, `XDG_*`) plus exactly the keys named there and drops every other pod variable, so anything a tool needs has to be listed. Currently the Kubernetes DNS variables, Hermes home, the Chat Pub/Sub config, the project ids, the Google Chat and Slack home channels, the API server key, the Session KV bearer token and database path, and the data root (`PLATFORM_AGENT_HOME`) the Cluster Agent roster tools read the profiles tree from. A home channel is what `send_notification` falls back to when a notification has no thread to reply into — every alert-driven investigation — and it needs `spec.integration.googleChat.homeChannel` set to carry a value; the bearer token is what lets it read `chat_id` and `thread_id` back in the first place, so an absent one costs the thread and the incident report both. See the comments on those keys in the source file.
- **`gke`** — Remote GKE MCP server, reached through `mcp-remote` and the credential proxy. `mcp-remote` sends each call to the proxy's MCP relay (the operator sets `KUBE_AGENTS_MCP_RELAY_URL` on the agent container) and presents the agent's projected token from the file `MCP_REMOTE_BEARER_FILE` names. The proxy relays only the server's read-only tools and refuses the rest, and forwards on its own credential, so the agent container needs none. `developer_knowledge` is reached the same way. `tools.include` registers only the server's read tools, the ones it annotates `readOnlyHint`, so its cluster, node-pool and manifest writes are never offered to the model; the proxy enforces the same list (`mcp_policy.GKE_READ_TOOLS`), so a write is refused even if the offer drifts. It is an allowlist on both sides: a tool the server adds later stays hidden and refused until it is named. A cluster profile already on the volume gets the `tools.include` list at the next pod start if it has none, and keeps the list it holds when a later image changes it; the proxy's list is what is enforced either way.

The two servers reached through `mcp-remote` — `gke` here, and `developer_knowledge` from the shared defaults — each pass `--header User-Agent: kube-agents/${KUBE_AGENTS_VERSION} (daemon)`. Without it the request reaches Google as undici's default `node` and the API team serving the endpoint cannot separate kube-agents traffic from anything else running on Node. Hermes resolves `${KUBE_AGENTS_VERSION}` in `args` from the agent process environment, where the image build put it — the commit the image was built from, or `dev` for a build that passed no version. `(daemon)` is the execution mode, carried in the RFC 9110 comment after the product token because that is where the API team's telemetry parser reads it from; every kube-agents profile is a background controller, so the value is the same for all of them. The string is the same everywhere it appears, including in the Cluster Agent template; it names the product, the build and the mode, and nothing else. A profile already on the volume when an image changes the string takes the new one at the next pod start: the entrypoint repairs the remote server entries in each cluster profile's `config.yaml`, and in the platform profile's when it is the front door, since the fill-only back-fill those two files get does not reach a value inside a list. For the two relayed servers that repair makes the whole `args` list and the token entry the image's — see [how config reaches each profile](/kube-agents/operator/platformagent-crd/#how-config-reaches-each-profile).

The two servers are timed out differently on purpose. `platform_control` gets `connect_timeout: 120` for cold-start latency — under `lazy` that bounds the first tool call rather than startup — and `timeout: 300` for long reasoning chains; it is a local subprocess, so a slow call is a slow call. `gke` gets `connect_timeout: 30` / `timeout: 60` because it is a remote endpoint reached through `mcp-remote`, where a failed call can consume the whole deadline without ever returning; the rationale is recorded in full alongside the block in [`agents/platform/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/config.yaml). Healthy calls to it measure under a second.

A variable the block omits arrives unset inside the server, and a read of it silently yields its default rather than failing — so a tool that appears to ignore a setting is usually reading a name the allowlist does not carry.

### `platform_toolsets`

Toolsets group MCP servers into named bundles for different Hermes surfaces:

- **`cli`** — Exposed to the Hermes CLI (interactive terminal usage inside the pod).
- **`api_server`** — Exposed to the Hermes REST API (Chat integrations, external callers).

Both include the same MCP servers plus their respective Hermes-native tools (`hermes-cli` / `hermes-api-server`), and both list `memory`. That entry is a gate, not a tool grant: Hermes only injects the memory provider's tools into a profile that names it, and a specialist reached as a kanban worker resolves its toolsets from `cli` while the API-server path uses `api_server`, so it has to appear on both for the two routes to see the same memory. Which tools it actually yields is decided by the provider — see [`memory`](#memory). `mcp-developer_knowledge` (a remote proxy to `developerknowledge.googleapis.com/mcp`) is declared in the shared defaults config ([`deploy/shared/defaults/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/deploy/shared/defaults/config.yaml)) and merged in at build time.

Note that the two files' toolset lists are **unioned**, not overridden — the build-time merge combines two lists as `list(dict.fromkeys(a + b))`. Removing an entry from `agents/platform/config.yaml` alone has no effect if the shared defaults still list it.

`agent.disabled_toolsets` then removes `browser`, `computer_use`, `image_gen`, `tts` and `vision` from both surfaces, on the Platform Agent and on every Cluster Agent. Hermes applies it last, so it also narrows the toolsets a kanban worker is spawned with. None of the agents' work uses them, and declaring the full set puts Gemini on Vertex on an intermittent slow path before the first token: in a replay of a captured worker request, the 90th-percentile wait fell from 13.7s (up to 57s) to 2.8s once they were gone.

No MCP server exposes an agent-to-agent call tool; delegation between agents is kanban-only.

### `toolsets`

A second, top-level gate distinct from `platform_toolsets`: listing `kanban` here exposes the kanban orchestrator tools (`kanban_create`, `kanban_list`, …) to the Platform Agent as a non-worker profile, so it can create and route delegation cards itself. Workers spawned by the dispatcher get the kanban tools automatically via `HERMES_KANBAN_TASK`.

### `agent`

`max_turns` is the per-turn tool-calling iteration budget of the runs this profile spawns without one of their own: the kanban workers, which is every delegated chat request and every issue-resolver card. It is pinned rather than left unset because an unset key is not a smaller default on the pinned Hermes: a worker is a `hermes chat` process whose own defaults cap it at 500, and a gateway turn reads no cap at all. 150 is 1.4 times the most tool calls a worker was seen to make across a full presubmit run and a full nightly run; the largest was an upgrades audit requested through chat, which runs inside a card and has no budget of its own. The fleet audits need more — the cost audit runs fourteen checks across every cluster and project and the drift audit twenty — so each audit carries `max_turns: 250` on its roster entry in `agents/platform/cron/jobs.json`, which the image's cron scheduler reads ahead of this value ([the patch](https://github.com/gke-labs/kube-agents/blob/main/deploy/docker/patches/apply_cron_job_max_turns.py)); before that patch the profile carried 250 and every worker ran with it. It is set here rather than in the operator's generated root config because both dispatch paths read the profile's `config.yaml`: kanban workers are spawned with `HERMES_HOME` pinned to the profile, and the cron scheduler falls back to `agent.max_turns` from `$HERMES_HOME/config.yaml`. The operator's `spec.harness.tuning.platform.maxTurns` overlays this value; a roster entry's own `max_turns` raises its job above whichever is in force and never lowers it, so an operator value above 250 reaches the audits too. The Planning Agent and the Cluster Agents leave the key unset, which on the pinned Hermes means no cap on a gateway turn and the chat CLI's 500 on a kanban worker. The comment in the file itself records the runs that motivated both numbers.

### `context_file_max_chars`

The character cap Hermes puts on each context file — `SOUL.md`, `AGENTS.md` — when it builds the system prompt. A file over the cap keeps only its first 70% and last 20% of the cap, and the middle is left out. Unset, the cap is 6% of the model window, and a flat 20,000 characters whenever the window is unknown or reported small. That happens on more than one path: a delegated subagent builds its prompt with no window at all, and a provider that reports a small window lowers the cap for every session after. This profile's `SOUL.md`, like the Planning Agent's, is well past 20,000 characters, so the setting is pinned to 100,000, which fits every shipped context file and is about 25,000 tokens at most. The Planning Agent's and Cluster Agent's configs pin the same value, because each profile reads its own `config.yaml`.

### `tool_loop_guardrails`

`loop_caps.max_web_searches` bounds how many `web_search` calls one turn may make. Hermes defaults to 50 and resets the counter in `reset_for_turn`, which is the right shape for an interactive session — fifty searches in a single turn there is pathological. A kanban worker is not that shape: outside goal mode the dispatcher spawns it with `chat -q`, so the whole card is one turn and the per-turn cap is really a per-card research budget. A genuine research card exhausts it, and the run ends where it was cut off.

Raised to 200 for this profile only, for the same reason as `max_turns` above: both dispatch paths read the profile's `config.yaml`, so the Planning Agent and the Cluster Agents keep the stock 50, which nothing has approached. 200 is a ceiling rather than a target — a card that reaches it is misbehaving and should be stopped, so do not set `0` (unlimited).

The cap was never the whole defect. What made hitting it expensive was the exit taken when it fired: the halt broke out of the agent loop without showing the model the block result, so a worker with 173 successful searches in hand was never told, never got another turn, and exited without closing its card. That path is repaired in [`deploy/docker/patches/kanban_guardrail_exit.py`](https://github.com/gke-labs/kube-agents/blob/main/deploy/docker/patches/kanban_guardrail_exit.py), whose module docstring carries the analysis. Raising the cap alone would only have moved when the failure happened.

`permission_refusal_halt_after` is a kube-agents addition, read by [`deploy/docker/patches/permission_refusal_stop.py`](https://github.com/gke-labs/kube-agents/blob/main/deploy/docker/patches/permission_refusal_stop.py): the number of definitive command-policy refusals in one run before the runtime halts it. The command policy's `kubernetes.read-only` rule refuses a kubectl write with a notice that the change is refused however it is attempted, and the credential proxy's client in the sandbox prints the verb it resolved beside the rule (`policy verb:`), which is what the runtime reads instead of parsing the command's shell text (the text is read only to tell a write chained before a successful read from a read that echoed a refusal); a refused write whose output the call discarded is not seen at all. The stock loop guardrails never see a workaround hunt, because they count identical retries and reset on any success. At the default of 1 the first refusal ends the run through the same kanban-aware exit, so a worker ends its card (blocked, or completed when the card asked for a pull request) with the refusal instead of running to its step budget, and a chat turn replies with the refusal. A higher value makes the refusals below it append guidance to the tool result instead; in live runs the model read that guidance and went on investigating, which is why the default is the first. An API 403 only warns and is never counted, so a fleet audit continues past a project it may not read; so do a `gcp.read-only` refusal and a `kubernetes.read-only` refusal of a read spelling such as `config view`, since both rules are allowlists that refuse an unlisted read with the same notice. There is no unlimited setting.

### `memory`

`memory_enabled: false` and `user_profile_enabled: false` switch off Hermes' built-in file store (`MEMORY.md` / `USER.md`). They are not what turns memory off here — they gate a different mechanism from the provider below, and leaving them on would hand this profile a writable file store alongside a provider designed to be read-only.

**Personal memory is impossible on this profile and always will be.** It keys off the gateway identity, and the Platform Agent is reached through the kanban dispatcher, which spawns workers with no human attached — so personal memory only makes sense on the [Planning Agent](/kube-agents/concepts/chatops/), the profile that actually receives chat ingress. The provider fails closed on that by itself: with no user identity it recalls `scope:shared` only.

**Shared memory is readable**, because withholding it did not stop this agent needing the corpus — it stopped it getting the corpus from a curated source. `read_only: true` is what keeps that one-way: it removes `memory_retain` from the schemas the model sees, refuses the call if it is made anyway, and turns off automatic capture. The measurement behind both decisions, and the reasoning for the read-only boundary, are in [`docs/designs/memory.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/memory.md).

`provider` is a default, not the decision. The choice belongs to the install — `--memory=` on `install.sh`, carried as `spec.harness.memory.provider` — and the operator overwrites this key from the CR through the profile's overlay, because this file is baked into the image. It writes an empty value for any provider that cannot be made read-only and scoped by tag, which is every provider except the two Hindsight-backed ones; a per-user file provider has no gateway identity to key on here. The value in the file is what a run without the operator gets: `multiuser_memory` on the Planning Agent, matching the CRD default, and empty on the Platform Agent, because that default is a provider a specialist cannot use. The field itself is documented in the [`PlatformAgent` CRD reference](/kube-agents/operator/platformagent-crd/); which providers are selectable, and what each costs to run, is in [`docs/designs/memory.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/memory.md).

### `plugins`

Hermes plugins enabled:

- **`hermes_otel`** — OpenTelemetry export. Its trace backend is baked into the image but rewritten at container start to the endpoint the operator resolved; see [Deploy → Telemetry](/kube-agents/deploy/telemetry/#pointing-at-your-own-collector).
- **`tool_call_audit`** — writes per-tool-call records for audit and debug.
- **`incident_context`** — injects Kubernetes incident context into known chat threads on reply (`pre_gateway_dispatch` hook). The work happens on the Planning Agent, which enables it too: the pod runs a single gateway and it is homed at that profile, so an incident-thread reply is dispatched there and never here. It stands aside for a message that starts with `/`: `legacy_slash_commands` is on the same hook, and prepending the triage report first would move the command off the front of the line where that plugin's anchored pattern can no longer see it.
- **`pr_opened`** — records each pull or merge request this profile's kanban workers open as a `pr_opened` event on their card (`transform_terminal_output` hook), once the forge confirms the agent's own credential opened it; with `KAGE_SLACK_UX` on, the gateway then posts it as its own message in the card's Slack thread ([ChatOps](/kube-agents/concepts/chatops/)). With [`platformFrontDoor`](/kube-agents/operator/platformagent-crd/#platformfrontdoor) on, the operator's overlay enables it, since the back-fill never adds an entry to a list the live file already holds.

The chat-ingress plugins — `session_store` (durable session state), `session_otel_bridge` (enriches OTel spans with session context, see [Session metadata](/kube-agents/concepts/observability/#session-metadata-plumbing)) and `chat_metrics` (counts inbound messages and failed turns, thumbs-down reactions and the board's cards that gave up for `status.usage`) — run on the Planning Agent profile, which owns chat ingress. Their sources live in [`agents/chat/defaults/plugins/`](https://github.com/gke-labs/kube-agents/tree/main/agents/chat/defaults/plugins).

## Related files

- [`agents/platform/SOUL.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/SOUL.md) — persona / system prompt.
- [`agents/platform/AGENTS.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/AGENTS.md) — workspace runtime instructions.
- [`agents/platform/cron/jobs.json`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/cron/jobs.json) — cron watchdog definitions. Advanced by `profile-cron-tick` on the Planning Agent's roster, which owns the only ticking gateway. See [Cron jobs reference](/kube-agents/reference/cron-jobs/).
