# Platform Session Management & Incident Triage Flow

This document details the architecture and workflow for routing GKE Kubernetes warning alerts into persistent diagnostic agent sessions, enabling interactive threaded troubleshooting in chat platforms (Google Chat and Slack).

---

## Architecture Overview

AI agent execution is typically stateless and triggered on-demand. To support proactive GKE warning troubleshooting, we run a local stateful proxy server called `session_kv_server.py` (the REST Bridge) on the Platform Agent host on `127.0.0.1:8699`.

This server acts as a bridge between the **GKE Event Watcher** (monitoring target clusters) and the **Platform Agent Gateway** (running the LLM reasoning turns).

It binds loopback rather than `0.0.0.0` because it has exactly three callers and all of them share this Pod's network namespace: the event watcher in the credential-proxy container, the Platform MCP server, and the `incident_context` plugin. Every route except `/healthz` also requires a bearer token from the `SESSION_KV_API_KEY` key of the agent's Secret — the rows it serves carry chat identifiers, and loopback inside a shared namespace is not on its own an authorization boundary. Deliberately not `API_SERVER_KEY`, which is the non-secret sentinel `cluster-internal-trusted` and would authenticate nothing. When the key is absent the server answers `503` to every authenticated route and logs why; see [the credential-isolation design](../../../docs/credential-isolation-design.md#the-loopback-only-exception).

### Key Responsibilities:

1. **Deduplication:** Maps repeat events to the same troubleshooting session, preventing alert flooding and saving LLM token costs.
2. **Dynamic Thread Resolution:** Captures the Chat API message ID returned from the first alert, saving it as the persistent thread key.
3. **Incident Triage Context Preservation:** Persists completed triage reports inside the local SQLite database.
4. **Gateway Message Rewriting Hook:** Integrates the `incident_context` plugin to intercept user replies on active incident threads and automatically prepend the triage report, allowing the fixer agent session to run with full context.
5. **Daily Alert Ceiling:** Caps how many alerts of each severity reach chat in one UTC day, bounding the volume that survives deduplication.

### Daily Alert Ceiling

Deduplication bounds how often _one_ failure is reported. It does nothing about many _distinct_ failures at once — a node draining or a namespace collapsing produces a hundred unrelated pods, each a legitimately new incident. The ceiling is the backstop for that case.

`inject_message` classifies severity (`get_severity_details`) and then spends one of that severity's daily allowance before anything is posted or any agent turn is started. This is the only place both actions pass through, and severity is not known any earlier — `POST /sessions` carries no payload.

| Severity   | Env var                      | Default |
| ---------- | ---------------------------- | ------- |
| `Critical` | `ALERT_DAILY_LIMIT_CRITICAL` | `10`    |
| `Warning`  | `ALERT_DAILY_LIMIT_WARNING`  | `5`     |
| `Info`     | `ALERT_DAILY_LIMIT_INFO`     | `5`     |

`Info` is capped alongside the others because it genuinely arrives. Nothing on the path from the kubelet to `inject_message` filters on `Event.Type`: the watcher's filter matches reason, namespace and repeat count, its informer carries no field selector, and the type is passed through in the payload. An allowlisted reason emitted as `type: Normal` is therefore classified `Info` here — `BackOff` is on the watcher's default reason list and the kubelet emits it as `Normal` for image-pull back-off, so an image-pull storm produces exactly that. Setting a limit to `0` turns that severity's cap off entirely.

All three are tunable on the `PlatformAgent` CR without rebuilding the image. They reach the container because they are on the sandbox env allowlist in `safeSandboxEnvOverrides` (`k8s-operator/internal/controller/platformagent_manifests.go`) — `spec.deployment.env` is filtered, so an arbitrary variable set there is dropped:

```yaml
spec:
  deployment:
    env:
      - name: ALERT_DAILY_LIMIT_CRITICAL
        value: "25"
      - name: ALERT_DAILY_LIMIT_WARNING
        value: "0" # uncapped
```

Behaviour worth knowing before relying on it:

- **Suppression is silent.** Nothing is posted to say the ceiling was reached — announcing it would spend a message to say no more messages are coming. The consequence is that once the cap bites, a quiet channel no longer distinguishes "nothing is wrong" from "the budget is spent", so the accounting lives outside chat: every suppressed alert is counted in `alert_quota`, logged at `WARNING` with the workload it dropped, and readable from `GET /v1/alert-quota`.
- **The counter is fleet-wide,** not per cluster. One collapsing cluster can therefore exhaust the day's budget for every other cluster.
- **It fails open.** If the quota table cannot be read or written, the alert goes through. A ceiling is a comfort feature and must never be the reason an incident is withheld.
- **The suppressed alert is still acknowledged** to the watcher with `200 {"status": "suppressed"}`. A failure response would leave the watcher's dedup entry unbound, so the same workload would be re-reported on its next sighting — a suppressed alert would cost more API calls than a delivered one.
- **The budget survives restarts,** because it is on the `system-metadata` PVC rather than in memory. A crash-looping session server would otherwise hand out a fresh day's quota on every restart, which is precisely the condition the cap exists for.
- **The day boundary is UTC midnight,** not the operator's local midnight.

---

## End-to-End Workflow

The diagram below details the lifecycles of alert ingestion, session routing, and interactive GitOps fixes:

```mermaid
sequenceDiagram
    autonumber
    participant K8s as GKE Target Cluster
    participant Watcher as k8s-event-watcher
    participant Proxy as session_kv_server (Port 8699)
    participant Gateway as Hermes Gateway (Port 8642)
    participant Front as Chat Agent (default profile)
    participant Board as Kanban Board
    participant Agent as Platform Agent LLM
    participant Chat as Google Chat / Slack
    participant Plugin as incident_context Plugin

    Note over K8s, Watcher: Phase 1: Alert Detection & Initialization
    K8s->>Watcher: Pod Eviction Warning (PDB Violation)
    Watcher->>Proxy: POST /sessions (Creates session ID: k8s-evt-abc123)
    Proxy-->>Watcher: Returns sessionID: k8s-evt-abc123
    Watcher->>Proxy: POST /sessions/k8s-evt-abc123/inject (Payload: Event details)
    Proxy->>Proxy: Spend one of today's alerts for this severity (silently drops if the ceiling is reached)
    Proxy->>Chat: Post Alert & Triage Report (N options, one marked Recommended)
    Note over Proxy: Store triage report in db (incidents table)
    Proxy->>Gateway: POST /api/sessions/k8s-evt-abc123/chat (Start Troubleshooter)
    Gateway->>Front: Serve the turn on `default` — the front-door Chat Agent
    Front->>Board: kanban_create(assignee: platform | cluster-*)
    Board->>Agent: Dispatch a worker as the assignee profile
    Agent-->>Board: RCA written to the card's result
    Board->>Front: Wake on completion (an API session has no thread to post into)
    Front->>Chat: send_notification(result, session_id) — threaded under the alert
    Note over Proxy: Warn if no incident row appears within the grace period

    Note over K8s, Watcher: Phase 2: Event Deduplication
    K8s->>Watcher: (Duplicate Warning Event occurs)
    Watcher->>Watcher: Detects active session cache for key
    Watcher->>Proxy: POST /sessions/k8s-evt-abc123/inject (Payload: count=5)
    Proxy->>Chat: Post threaded repeat warning message

    Note over Agent, Chat: Phase 3: Reporting & Human-in-the-Loop Resolution
    Chat->>Plugin: User replies: "apply" (recommended) or "apply Option B" (Hook: pre_gateway_dispatch)
    Plugin->>Proxy: GET /v1/incidents/by-thread
    Proxy-->>Plugin: Return triage report content
    Note over Plugin: Rewrite message text to prepend triage report context
    Plugin->>Gateway: Spawn Fixer Agent with rewritten message
    Gateway->>Agent: Inject context into conversation turn
    Agent->>Agent: Create branch, edit git manifests, open GitOps PR
    Agent->>Chat: Post threaded reply "Created PR #334"
```

---

## Who Delivers the Report

An event alert reaches the gateway on an unprefixed path, so the turn is served by
`default` — the front-door Chat Agent — which reads the alert and delegates it to a
specialist with `kanban_create`. That is the intended shape: the Chat Agent decides where
work goes.

Delivery is the part that needs care, because **the report is not posted by whoever wrote
it**. Kanban normally posts a finished card's `result` into the chat thread the card came
from. An event alert has no such thread: it arrived over the API server, and the thread it
belongs to is recorded in `session_metadata`, not in the card. So the board stays quiet and
wakes the card's creator instead, and that turn's own post is the delivery
(`kanban.wake_on_events` in `agents/chat/config.yaml`).

The creator is always the Chat Agent, whatever the assignee. It therefore needs an egress of
its own, and has exactly one: `send_notification`, on the `router` MCP server it already
runs. Given the alert's session id the tool resolves `chat_id`/`thread_id` from
`session_metadata`, posts there, and writes the `incidents` row. Without a session id it
falls back to the home channel — the report lands, but not under the alert it answers.

The tool is on `router` rather than granted as `mcp-platform_control` deliberately. The
Platform Agent's server also carries the provisioning and Config Connector surface, and the
whole design of the front door is that it holds no infrastructure tools. One tool on the
server it already has is the smaller grant.

Two consequences:

- **A Cluster Agent cannot deliver its own RCA and is not expected to.** `platform_control`
  is deliberately absent from `agents/cluster/config.yaml` — a Cluster Agent diagnoses, it
  does not provision — so its report reaches the user only via the Chat Agent's relay.
- **A wedged board loses the report before delivery is ever reached.** Workers are capped by
  `kanban.max_in_progress`, and tasks left `running` when the pod dies are not reclaimed. Two
  orphans against the default cap of 2 and the dispatcher spawns nothing at all. `hermes
  kanban stats` shows a non-empty ready queue with no runners; `hermes kanban reclaim <id>`
  frees the slot.

### Detecting an Undelivered Report

`send_notification` is the only writer of the `incidents` table, so a row is the one durable
proof the RCA reached a human. After starting the turn the daemon watches for that row and
logs a `WARNING` naming the session and thread if none appears within
`TRIAGE_DELIVERY_GRACE_SECONDS` (default 900s, polled every
`TRIAGE_DELIVERY_POLL_SECONDS`). It waits rather than checking once because the Chat Agent
answers as soon as it has filed the card, minutes before the worker finishes.

A warning, not an error: the daemon cannot distinguish a lost report from a slow one with
certainty, and the alert itself has already been posted either way. The point is that the
silent case is no longer silent — a triage that loses its report used to be byte-identical
to one that succeeded at every layer.

### How Long a Turn May Take

`TRIAGE_TURN_TIMEOUT_SECONDS` (default 1800) bounds the synchronous call to the gateway.
It is a whole agent reasoning loop, not an HTTP round trip; the previous 300s ceiling fired
mid-investigation and took the report with it. The elapsed time is logged on both the success
and failure paths, because without it a timeout is indistinguishable from a hung gateway.

---

## Database Schemas & Storage

Session and incident data are stored in a local SQLite database inside the Platform Gateway pod:

```text
/var/lib/kube-agents/session/session_kv.db
```

### Table Schemas

#### `session_metadata`

Stores the mapping between the troubleshooter session and the platform chat thread:

```sql
CREATE TABLE session_metadata(
  session_id TEXT PRIMARY KEY,
  metadata TEXT NOT NULL,         -- JSON object storing platform, chat_id, thread_id, and timestamps
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

#### `incidents`

Stores the triage report context for active incident threads:

```sql
CREATE TABLE incidents(
  chat_id TEXT,
  thread_id TEXT,
  report TEXT NOT NULL,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (chat_id, thread_id)
);
```

#### `alert_quota`

Tracks how much of each severity's daily allowance has been spent, and how many alerts the ceiling dropped:

```sql
CREATE TABLE alert_quota(
  day TEXT NOT NULL,              -- UTC YYYY-MM-DD
  severity TEXT NOT NULL,         -- Critical | Warning | Info
  sent INTEGER NOT NULL DEFAULT 0,
  suppressed INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, severity)
);
```

Rows age out with everything else after `SESSION_KV_CLEANUP_TTL_DAYS`, so roughly two weeks of history is available to answer "what did we drop last week".

---

## Verification & Troubleshooting

### Check Today's Alert Budget

Suppression is silent in chat, so this is how you tell a quiet day from a capped one:

```bash
kubectl -n kubeagents-system exec deployment/platform-agent-gateway -c platform-agent -- \
  sh -c 'curl -s -H "Authorization: Bearer $SESSION_KV_API_KEY" \
    http://127.0.0.1:8699/v1/alert-quota'
```

Pass `?day=YYYY-MM-DD` for a past day. To see which workloads were dropped:

```bash
kubectl -n kubeagents-system logs deployment/platform-agent-gateway -c platform-agent | grep "Suppressed"
```

### Check Persisted Incidents

To view currently registered incident triage reports:

```bash
kubectl -n kubeagents-system exec deployment/platform-agent-gateway -c platform-agent -- \
  sqlite3 /var/lib/kube-agents/session/session_kv.db "SELECT chat_id, thread_id, updated_at FROM incidents;"
```

### Verify Inbound Plugin Activity

Filter container logs to trace whether the `incident_context` plugin is successfully intercepting threads and rewriting messages:

```bash
kubectl -n kubeagents-system logs deployment/platform-agent-gateway -c platform-agent | grep -E "incident_context|inbound message"
```
