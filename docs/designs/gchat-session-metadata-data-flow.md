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
user_email_hash
user_resource
chat_id
thread_id
updated_at
```

These keys are platform-neutral. For Google Chat, the Chat space is stored as
`chat_id`; there is no separate `google_chat_id` key.

No plaintext identity is stored. `user_email_hash` is an HMAC-SHA256 digest
salted with `SESSION_KV_SALT`, and `user_id` gets the same treatment whenever it
looks like an address — which on Google Chat it always does, because the Chat
"user id" _is_ the address. A Slack member id contains no `@`, is already a
pseudonym, and stays readable so an operator can still resolve it against the
Slack directory. The hash is stable for as long as the salt is, so sessions from
the same person still correlate; the salt is generated once at install and
rotating it re-anonymizes everyone. `agents/chat/defaults/plugins/common/redactor.py`
is the single implementation, shared by the store, the OTel bridge, and both
audit hooks.

Rows written before this change still carry `user_email`, so
`session_kv_server.init_db()` strips the plaintext keys out of them on startup.
It rewrites the row rather than deleting it: `chat_id` and `thread_id` are what
route a threaded reply, and dropping the row would break every conversation
already in flight. The hash reappears on that user's next message.

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

Two things start this resolver, and neither is a Kubernetes sidecar of its own:

- `platform_mcp_server.py` starts it when the platform MCP server starts.
- The container entrypoint (`deploy/shared/docker-entrypoint.sh`) starts it on port
  8699 — but only in the gateway container. Port 8699 must have exactly one owner:
  the Deployment runs the same image in several containers that share one pod network
  namespace, so a second container reaching that step binds a port already taken. The
  entrypoint's shared-state gate (step 1.5) is what keeps the launch to a single
  container: the operator sets `AGENT_SHARED_STATE_SETUP=owner` on the gateway and
  `skip` on every sidecar, and a container that does not own the shared state execs its
  own command before reaching this step. See
  [Container entrypoint](/kube-agents/deploy/docker-images/#container-entrypoint).

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
  "user_id": "9f2c...b41e",
  "user_email_hash": "9f2c...b41e",
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
  "user.id": "google_chat:9f2c...b41e",
  "hermes.sender.id": "9f2c...b41e",
  "chat.id": "spaces/REDACTED",
  "chat.platform": "google_chat"
}
```

## Delegation

> **STATUS — not mounted.** This section describes the `call_agent` A2A path, whose
> MCP server is no longer declared for any profile. It could not reach the Platform
> Agent in this deployment and was removed rather than repaired; see the note above
> `mcp_servers` in `deploy/shared/defaults/config.yaml`. Delegation is kanban-only.
> The header contract below is kept as the reference design: `SessionManager` and its
> signing scheme remain in `agents/platform/scripts/session_manager.py`, and any future
> synchronous path should honour it.

When `agent_common_server.py` delegates to another agent, it uses
`SessionManager` to forward the same session context as cryptographically
signed headers:

```text
X-Hermes-Session-Id
X-Hermes-User-Id
X-Hermes-Sender-Id
X-Hermes-User-Email-Hash
X-Hermes-Chat-Id
X-Hermes-Thread-Id
X-Hermes-Signature
X-Hermes-Timestamp
```

`X-Hermes-User-Email-Hash` carries the digest, not the address. It replaced
`X-Hermes-User-Email` rather than being added alongside it: that header existed
so a downstream agent could show an operator who asked, and a hash cannot do
that — the pseudonymous `X-Hermes-Sender-Id` already correlates turns from the
same person, which is the part downstream consumers actually use.

This allows downstream agents to preserve attribution when they receive the
session context. As a future requirement, downstream consumers will be required
to cryptographically verify the HMAC-SHA256 signature in `X-Hermes-Signature`
against the delegation signing secret (`HERMES_DELEGATION_SIGNING_KEY` or derived
secret) and validate timestamp freshness before trusting the session context.
The signing payload covers the timestamp, session ID, target agent ID, body digest,
and length-prefixed canonicalized header digest.

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

## Reliability & Security Notes

- The authoritative ingress mapping uses Hermes runtime session state, not a
  model-supplied tool parameter.
- The raw Google Chat event does not carry a Hermes `session_id`; the mapping is
  created after Hermes resolves the session.
- Attribution is limited to fixed fields we explicitly persist and format:
  `session_id`, Google Chat sender identity, Google Chat space/thread, and
  delegation headers. The code does not dynamically parse arbitrary attributes
  for user identity.
- Signed delegation headers (`X-Hermes-Signature` via HMAC-SHA256 and
  `X-Hermes-Timestamp`) are emitted when forwarding session context across
  inter-agent delegation hops using a dedicated signing secret
  (`HERMES_DELEGATION_SIGNING_KEY` or derived key), binding the timestamp,
  session ID, target agent ID, body digest, and length-prefixed canonicalized
  header digest; cryptographic verification and timestamp validation are
  requirements on downstream consumers that are not yet met.
- OTel enrichment depends on `hermes_otel`, `session_store`, and
  `session_otel_bridge` all being enabled.
- Remote systems can only preserve attribution if they receive, verify, and honor the
  forwarded session headers.
