# Platform Session Management & Incident Triage Flow

This document details the architecture and workflow for routing GKE Kubernetes warning alerts into persistent diagnostic agent sessions, enabling interactive threaded troubleshooting in chat platforms (Google Chat and Slack).

---

## Architecture Overview

AI agent execution is typically stateless and triggered on-demand. To support proactive GKE warning troubleshooting, we run a local stateful proxy server called `session_kv_server.py` (the REST Bridge) on the Platform Agent host on port `8699`.

This server acts as a bridge between the **GKE Event Watcher** (monitoring target clusters) and the **Platform Agent Gateway** (running the LLM reasoning turns).

### Key Responsibilities:

1. **Deduplication:** Maps repeat events to the same troubleshooting session, preventing alert flooding and saving LLM token costs.
2. **Dynamic Thread Resolution:** Captures the Chat API message ID returned from the first alert, saving it as the persistent thread key.
3. **Incident Triage Context Preservation:** Persists completed triage reports inside the local SQLite database.
4. **Gateway Message Rewriting Hook:** Integrates the `incident_context` plugin to intercept user replies on active incident threads and automatically prepend the triage report, allowing the fixer agent session to run with full context.

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
    participant Agent as Platform Agent LLM
    participant Chat as Google Chat / Slack
    participant Plugin as incident_context Plugin

    Note over K8s, Watcher: Phase 1: Alert Detection & Initialization
    K8s->>Watcher: Pod Eviction Warning (PDB Violation)
    Watcher->>Proxy: POST /sessions (Creates session ID: k8s-evt-abc123)
    Proxy-->>Watcher: Returns sessionID: k8s-evt-abc123
    Watcher->>Proxy: POST /sessions/k8s-evt-abc123/inject (Payload: Event details)
    Proxy->>Chat: Post Alert & Triage Report (Option A & B)
    Note over Proxy: Store triage report in db (incidents table)
    Proxy->>Gateway: POST /api/sessions/k8s-evt-abc123/chat (Start Troubleshooter)
    Gateway->>Agent: Wake up troubleshooter agent

    Note over K8s, Watcher: Phase 2: Event Deduplication
    K8s->>Watcher: (Duplicate Warning Event occurs)
    Watcher->>Watcher: Detects active session cache for key
    Watcher->>Proxy: POST /sessions/k8s-evt-abc123/inject (Payload: count=5)
    Proxy->>Chat: Post threaded repeat warning message

    Note over Agent, Chat: Phase 3: Reporting & Human-in-the-Loop Resolution
    Chat->>Plugin: User replies: "apply Option B" (Hook: pre_gateway_dispatch)
    Plugin->>Proxy: GET /v1/incidents/by-thread
    Proxy-->>Plugin: Return triage report content
    Note over Plugin: Rewrite message text to prepend triage report context
    Plugin->>Gateway: Spawn Fixer Agent with rewritten message
    Gateway->>Agent: Inject context into conversation turn
    Agent->>Agent: Create branch, edit git manifests, open GitOps PR
    Agent->>Chat: Post threaded reply "Created PR #334"
```

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

---

## Verification & Troubleshooting

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
