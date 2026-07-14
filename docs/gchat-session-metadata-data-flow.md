# Google Chat Session Metadata Data Flow

This document describes the attribution path from a Google Chat message across OpenClaw's gateway, Python tool servers, cross-agent delegation hops, and OpenTelemetry spans.

## Overview

In the OpenClaw architecture, Google Chat webhooks arrive over HTTPS at the compiled Node.js Gateway. The raw event contains sender identity and Chat conversation metadata (`space.name`, `thread.name`), but does not initially carry an OpenClaw `session_id`.

```text
Google Chat HTTPS Webhook (`/googlechat`)
  -> OpenClaw Node.js Gateway (resolves `session_id`, `user.id`)
  -> Environment variables passed to Python tool servers / MCP subprocesses (`OPENCLAW_SESSION_ID`, `OPENCLAW_USER_ID`)
  -> SessionManager / session_store (`/var/lib/kube-agents/session/session_kv.db`)
  -> X-OpenClaw-* Delegation Headers (cross-agent calls)
  -> session_otel_bridge (Python OTel span attributes)
```

## Components & Flow

### 1. OpenClaw Node.js Gateway

When an inbound HTTPS POST request arrives at `/googlechat`:

1. OpenClaw verifies the webhook target (`audienceCheck` against `openclaw.json`).
2. Extracts the sender identity (`sender.name` / `email`) and Chat conversation (`space.name`, `thread.name`).
3. Establishes the authoritative `session_id` for the conversation turn.
4. When launching local MCP servers or Python tool subprocesses (`platform_mcp_server.py`, `agent_common_server.py`), OpenClaw passes down active session context via environment variables (`OPENCLAW_SESSION_ID`, `OPENCLAW_USER_ID`, `OPENCLAW_SENDER_ID`).

### 2. SessionManager (`session_manager.py`)

`SessionManager` is the universal Python session resolver used across our multi-agent harness (`agent_common_server.py`, peer agents, and tool runners).

When `SessionManager.current_context()` is invoked, it resolves the caller's identity in prioritized order:

1. **Environment Variables:** Checks `OPENCLAW_SESSION_ID`, `OPENCLAW_USER_ID`, and `OPENCLAW_SENDER_ID` (with fallback to legacy `HERMES_` keys).
2. **SQLite Session Store (`session_kv.db`):** If an explicit `session_id` is provided or found in environment/headers, it queries `/var/lib/kube-agents/session/session_kv.db` (`session_metadata` table) to retrieve stored metadata (`platform`, `user_id`, `user_email`, `chat_id`, `thread_id`).

### 3. session_store (`session_store/store.py`)

`session_store` (`SessionMetadataStore` and `SessionMetadata`) manages persistent SQLite (`/var/lib/kube-agents/session/session_kv.db`) storage for Python-based agent layers, peer agents (`DevTeamAgent`, `OperatorAgent`), or Python gateway pipelines.

When active, it:

1. Builds a `SessionMetadata` object from event sources (`platform`, `user_id`, `chat_id`, `thread_id`).
2. Writes `session_id -> metadata` JSON into `/var/lib/kube-agents/session/session_kv.db`.

For session storage, it keeps only this fixed metadata allowlist:

```text
session_id
platform
user_id
user_email
user_resource
chat_id
thread_id
updated_at
```

### 4. session_otel_bridge (`session_otel_bridge/bridge.py`)

`session_otel_bridge` (`OtelSessionBridge`) enriches Python OpenTelemetry spans with user attribution.

At initialization, it installs a wrapper around Python `tracer.start_span`. For each span:

1. Reads `session_id` passed to `start_span` (or active session context).
2. Reads the matching metadata row from `/var/lib/kube-agents/session/session_kv.db`.
3. Injects fixed identity attributes into the OTel span:

| Attribute | Description | Example |
| :--- | :--- | :--- |
| `session.id` | Authoritative session identifier for the conversation turn | `20260702_153830_50074bf0` |
| `user.id` | Composite identity prefixed with platform (`platform:email`) | `google_chat:user@example.com` |
| `openclaw.sender.id` | Primary sender identity across OpenClaw components | `user@example.com` |
| `hermes.sender.id` | Legacy sender identity (preserved for backwards compatibility) | `user@example.com` |
| `chat.id` | Target chat space or channel identifier | `spaces/REDACTED` |
| `chat.thread_id` | Specific conversation thread ID (if applicable) | `threads/REDACTED` |
| `chat.platform` | Originating messaging channel (`google_chat`, `slack`) | `google_chat` |

Example attributes, anonymized:

```json
{
  "session.id": "20260702_153830_50074bf0",
  "user.id": "google_chat:user@example.com",
  "openclaw.sender.id": "user@example.com",
  "hermes.sender.id": "user@example.com",
  "chat.id": "spaces/REDACTED",
  "chat.platform": "google_chat"
}
```

### 5. session_kv_server (`scripts.session_kv_server:app`)

`platform_mcp_server.py` starts a local HTTP resolver (`start_session_kv_server()`) on port 8699 exposing `session_kv.db` queries:

```text
GET /v1/sessions/{session_id}/metadata
GET /v1/sessions
GET /healthz
```

## Cross-Agent Delegation

When `agent_common_server.py` delegates to another agent over HTTP/MCP, it invokes `SessionManager.delegation_headers(context)` to forward the resolved attribution as HTTP headers:

### Primary OpenClaw Headers

```text
X-OpenClaw-Session-Id: 20260702_153830_50074bf0
X-OpenClaw-User-Id: google_chat:user@example.com
X-OpenClaw-Sender-Id: user@example.com
X-OpenClaw-User-Email: user@example.com
X-OpenClaw-Chat-Id: spaces/REDACTED
X-OpenClaw-Thread-Id: threads/REDACTED
```

### Legacy Hermes Headers (Dual-Emitted)

To maintain backwards compatibility across older downstream peers, legacy headers are emitted in parallel:

```text
X-Hermes-Session-Id: 20260702_153830_50074bf0
X-Hermes-User-Id: google_chat:user@example.com
X-Hermes-Sender-Id: user@example.com
X-Hermes-User-Email: user@example.com
X-Hermes-Chat-Id: spaces/REDACTED
X-Hermes-Thread-Id: threads/REDACTED
```

When the downstream target agent receives these headers, its local `SessionManager` reads them from the request environment/headers, preserving exact attribution across multi-agent hops without requiring direct network access to the upstream cluster's SQLite file.

## Stored Data Schema

SQLite database:

```text
/var/lib/kube-agents/session/session_kv.db
```

Table:

```text
session_metadata(
  session_id TEXT PRIMARY KEY,
  metadata TEXT NOT NULL,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

Example stored row:

```json
{
  "session_id": "20260702_153830_50074bf0",
  "platform": "google_chat",
  "user_id": "user@example.com",
  "user_email": "user@example.com",
  "user_resource": "users/REDACTED",
  "chat_id": "spaces/REDACTED",
  "thread_id": "",
  "updated_at": "2026-07-02T18:22:31Z"
}
```

## Verification

Check the persisted session mapping:

```bash
kubectl -n kubeagents-system exec "$POD" -c platform-agent -- \
  /opt/openclaw/.venv/bin/python3 - <<'PY'
import json, sqlite3

with sqlite3.connect("/var/lib/kube-agents/session/session_kv.db") as conn:
    rows = conn.execute(
        """
        SELECT session_id, metadata, updated_at
        FROM session_metadata
        ORDER BY updated_at DESC
        LIMIT 10
        """
    )
    for session_id, metadata, updated_at in rows:
        print(session_id, updated_at)
        print(json.dumps(json.loads(metadata), indent=2))
PY
```

Check local OTel rows:

```bash
SESSION_ID="<session_id>"

kubectl -n kubeagents-system exec "$POD" -c platform-agent -- \
  env SESSION_ID="$SESSION_ID" /opt/openclaw/.venv/bin/python3 - <<'PY'
import json, os, sqlite3

session_id = os.environ["SESSION_ID"]

with sqlite3.connect("/opt/data/plugins/openclaw_otel/live.db") as conn:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT seq, kind, data FROM events WHERE data LIKE ? ORDER BY seq",
        (f"%{session_id}%",),
    )
    for row in rows:
        data = json.loads(row["data"])
        attrs = data.get("attrs") or data.get("attributes") or {}
        print(json.dumps({
            "seq": row["seq"],
            "kind": row["kind"],
            "name": data.get("name"),
            "trace_id": data.get("trace_id"),
            "span_id": data.get("span_id"),
            "attrs": attrs,
        }, sort_keys=True))
PY
```

Check Cloud Trace export by `trace_id`:

```bash
PROJECT_ID="<project>"
TRACE_ID="<trace_id>"

curl -s \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://cloudtrace.googleapis.com/v1/projects/${PROJECT_ID}/traces/${TRACE_ID}" \
  | jq '.spans[] | {name, spanId, labels}'
```

## Reliability Notes

- The primary ingress entrypoint (`PlatformAgent`) resolves identity via the OpenClaw Node.js gateway and propagates it to Python subprocesses via environment variables (`OPENCLAW_SESSION_ID`, `OPENCLAW_USER_ID`).
- `session_kv.db` serves as the shared SQLite persistence layer across Python tool servers, multi-agent delegation resolvers (`SessionManager`), and OpenTelemetry bridges (`OtelSessionBridge`).
- Attribution is strictly restricted to the fixed fields explicitly defined in `SessionMetadata` (`session_id`, sender identity, chat space/thread, and delegation headers). The code does not dynamically scan arbitrary payloads or tool arguments to discover identity.
- Dual-emitting both `X-OpenClaw-*` and `X-Hermes-*` delegation headers ensures zero-breakage interoperability across hybrid or rolling multi-agent deployments.
