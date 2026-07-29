# Google Chat Session Metadata Data Flow

This document describes the final attribution path from a Google Chat message to
Hermes OpenTelemetry spans.

## Overview

The raw Google Chat Pub/Sub event contains the sender and Chat conversation
metadata, but it does not contain a Hermes `session_id`.

Hermes creates or resolves the `session_id` after the Google Chat adapter
converts the raw event into a gateway message. The `session_store` plugin then
persists the mapping from that Hermes session to the Google Chat sender. The
`session_otel_bridge` plugin uses that mapping to add fixed identity attributes
to Hermes OTel spans.

```text
Google Chat event
  -> Hermes Google Chat adapter
  -> Hermes session_id
  -> session_store plugin
  -> /var/lib/kube-agents/session/session_kv.db
  -> session_otel_bridge
  -> Hermes OTel span attributes
```

## Components

### session_store

`session_store` is a Hermes plugin enabled in `agents/platform/config.yaml`.
It registers the `pre_gateway_dispatch` hook.

On each gateway message, it:

1. Reads `event.source` from the parsed Hermes message.
2. Calls Hermes `session_store.get_or_create_session(source)`.
3. Reads the resulting Hermes `session_id`.
4. Builds a plugin-local `SessionMetadata` object from `event.source`.
5. Writes `session_id -> metadata` into `/var/lib/kube-agents/session/session_kv.db`.

The plugin does not create spans and does not modify OTel.

### SessionMetadata

`SessionMetadata` is a plugin-local class in `session_store`. It defines the
fixed metadata retained for a Hermes session.

It owns:

- the fixed session metadata allowlist
- conversion from Hermes `event.source` to stored metadata

It does not scan arbitrary dictionaries, tool arguments, span attributes, or
model-provided payloads to discover identity.

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

These keys are platform-neutral. For Google Chat, the Chat space is stored as
`chat_id`; there is no separate `google_chat_id` key.

### session_otel_bridge

`session_otel_bridge` is a Hermes plugin enabled after `hermes_otel`.

At plugin registration time, it installs a wrapper around the Hermes OTel
tracer's `start_span` method. For each span, the wrapper:

1. Reads the explicit `session_id` argument passed to `start_span`.
2. Reads the matching metadata row from `session_kv.db`.
3. Maps that metadata to the bridge-owned fixed span attribute allowlist.
4. Calls the original Hermes OTel `start_span`.

The bridge intentionally does not infer identity from existing span attributes
or other dynamic payloads. It only uses the explicit `session_id` passed by
Hermes OTel.

### session_kv_server

`session_kv_server.py` exposes a small HTTP resolver for the same
`session_kv.db` data:

```text
GET /v1/sessions/{session_id}/metadata
GET /v1/sessions
GET /healthz
```

`platform_mcp_server.py` starts this resolver when the platform MCP server
starts. There is no separate Kubernetes sidecar for it.

## Stored Data

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

Example metadata, anonymized:

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

## OTel Attributes

When a matching session row exists, `session_otel_bridge` adds these fixed
attributes to Hermes OTel spans:

```text
session.id
user.id
hermes.sender.id
chat.id
chat.thread_id
chat.platform
```

Example attributes, anonymized:

```json
{
  "session.id": "20260702_153830_50074bf0",
  "user.id": "google_chat:user@example.com",
  "hermes.sender.id": "user@example.com",
  "chat.id": "spaces/REDACTED",
  "chat.platform": "google_chat"
}
```

## Delegation

When `agent_common_server.py` delegates to another agent, it uses
`SessionManager` to forward the same session context as headers:

```text
X-Hermes-Session-Id
X-Hermes-User-Id
X-Hermes-Sender-Id
X-Hermes-User-Email
X-Hermes-Chat-Id
X-Hermes-Thread-Id
```

This allows downstream agents to preserve attribution when they receive the
session context.

## Verification

Check the persisted session mapping:

```bash
kubectl -n kubeagents-system exec "$POD" -c platform-agent -- \
  /opt/hermes/.venv/bin/python3 - <<'PY'
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

Check local Hermes OTel rows:

```bash
SESSION_ID="<session_id>"

kubectl -n kubeagents-system exec "$POD" -c platform-agent -- \
  env SESSION_ID="$SESSION_ID" /opt/hermes/.venv/bin/python3 - <<'PY'
import json, os, sqlite3

session_id = os.environ["SESSION_ID"]

with sqlite3.connect("/opt/data/plugins/hermes_otel/live.db") as conn:
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

- The authoritative ingress mapping uses Hermes runtime session state, not a
  model-supplied tool parameter.
- The raw Google Chat event does not carry a Hermes `session_id`; the mapping is
  created after Hermes resolves the session.
- Attribution is limited to fixed fields we explicitly persist and format:
  `session_id`, Google Chat sender identity, Google Chat space/thread, and
  delegation headers. The code does not dynamically parse arbitrary attributes
  for user identity.
- OTel enrichment depends on `hermes_otel`, `session_store`, and
  `session_otel_bridge` all being enabled.
- Remote systems can only preserve attribution if they receive and honor the
  forwarded session headers.
