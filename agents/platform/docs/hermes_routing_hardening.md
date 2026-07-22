# Hardening the hermes `gateway_routing` integration

Status: **Fixes 1–3 landed in this PR. Items 4–8 are scoped backlog (not yet implemented).**

## Context

PR #329 adds a background `k8s-event-watcher` that streams GKE warning events and
POSTs them to the local session bridge (`session_kv_server.py`). PR #333 wires that
into the Platform Agent, including **bidirectional chat** — a human reply in the
alert thread must drive the agent (phase-1 requirement).

For a normal, human-initiated conversation, **hermes** (the external chat gateway,
owned by another team, not in this repo) ingests the first message and creates the
reply-routing entry itself. A watcher-initiated thread is *proactive*: hermes never
saw an inbound message, so its routing table has no entry, so replies won't route
back to the agent. PR #333 solves this by **writing hermes's private
`gateway_routing` table directly** (`register_gateway_routing()` in
`session_kv_server.py`, DB at `STATE_DB_PATH` = `/opt/data/state.db`).

**hermes is a hard external dependency we cannot change on our timeline.** The clean
fix (get hermes to expose a `--bind-session` flag / proactive-thread API so it owns
the routing) is out of scope for now, so we are writing its table directly and
hardening that write.

## Why this needs more care than normal "ship and iterate"

The usual reason it's safe to merge fast and iterate is that the blast radius is your
own service. Here it is not:

1. **We write into another team's live database.** A bad write can corrupt or hijack
   real user conversations, and a hermes schema migration can silently break us.
2. **The row shape is persisted into hermes's table.** Getting it wrong now means
   rows hermes can't route *and* a later migration of already-written foreign rows.

So the gate is not *merge* — it's *prod exposure*. Anything that can damage hermes's
data or gets baked into its table must land before this routes real prod traffic.
Everything else is genuinely deferrable.

---

## The full fix set

### Landed in this PR (required before prod)

All three live in `register_gateway_routing()` in
`agents/platform/scripts/session_kv_server.py`, covered by
`TestRegisterGatewayRouting` in `test_session_kv_server.py`.

#### Fix 1 — `ON CONFLICT DO UPDATE`, scoped to `k8s-evt-` rows
- **What:** Replaced `INSERT OR REPLACE` with
  `INSERT ... ON CONFLICT(session_key) DO UPDATE`, and refuse to overwrite any row
  whose `entry_json.session_id` is not one of our `k8s-evt-` sessions.
- **Why:** `INSERT OR REPLACE` is a delete+reinsert — it wipes any hermes-managed
  columns on the row. The scoping guard prevents us from ever hijacking a real
  human-initiated routing entry that happens to collide on `session_key`.
- **Risk if skipped:** Corrupting/overwriting another team's live routing rows.
- **Assumption to confirm:** `session_key` has a UNIQUE/PK constraint (required for
  the `ON CONFLICT` target). Verify against the real schema.

#### Fix 2 — Template-from-a-real-row + schema-drift refusal
- **What:** Before writing, read the actual `gateway_routing` columns; if any column
  we own is missing, **refuse to write** (schema drift). For columns we don't own,
  copy the values from a real existing row (preferring one for the same platform) so
  our synthetic row matches whatever hermes currently writes.
- **Why:** We can't see hermes's schema at build time. Inheriting from a live row
  keeps us conformant even if hermes adds columns, instead of persisting a shape
  hermes silently can't route.
- **Risk if skipped:** Rows hermes can't route (broken replies) + migrating
  already-written malformed foreign rows later.
- **Note:** If the table is empty (no template available) we log a loud warning and
  write owned columns only — a manual diff against a real row is still worth doing
  once in the target environment.

#### Fix 3 — `busy_timeout`, don't fight hermes's journal mode
- **What:** Set `PRAGMA busy_timeout=5000` on the gateway connection; do **not** set
  `journal_mode` (leave whatever hermes configured on the file intact).
- **Why:** It's a shared live SQLite file with hermes as the other writer. A generous
  busy timeout makes us queue behind hermes's writers instead of erroring with
  "database is locked"; forcing a journal mode would fight hermes for ownership of a
  file-level property.
- **Risk if skipped:** Intermittent "database is locked" failures under concurrency.

### Fast-follow (safe to defer, file a ticket + monitor)

#### Fix 4 — Observability: metrics + loud schema-drift alert
- **What:** Counters for routing-write outcomes (`success`, `refused_foreign`,
  `refused_schema_drift`, `error`, `alert_send_failure`, `template_missing`), exposed
  via a small JSON endpoint (no new deps; Prometheus is only used on the Go side
  today). Alert on `refused_schema_drift > 0` and on write errors.
- **Why:** The Fix 2 refusal is our early-warning system for a hermes migration — but
  only if someone is watching. Without a signal, a drift refusal looks identical to
  "no incidents."
- **Deferrable because:** Fix 2 already fails safe (refuses) rather than corrupting.

#### Fix 5 — Saga ordering + reconciler for partial failure
- **What:** Treat alert→session→routing as one saga. Record routing state in
  `session_metadata` (`thread_id`, `platform`, `chat_id`, `routing_registered`).
  Retry the routing write with backoff; on repeated failure, post a follow-up
  in-thread notice ("live replies not enabled") and mark for reconciliation. Add a
  periodic reconciler that re-attempts routing for sessions with a thread but
  `routing_registered = false`.
- **Why:** Today, if the alert posts but the routing write fails, human replies
  vanish with no repair path (the dual-DB partial-failure hole).
- **Deferrable because:** Degrades visibly at low event volume; acceptable to
  fast-follow while watching.

### Later (pure internal refactor — fully reversible)

#### Fix 6 — `HermesRoutingAdapter` boundary
- **What:** Put the gateway write behind a one-method interface,
  `register_thread(session_id, platform, chat_id, thread_id)`, with a direct-SQLite
  implementation. When hermes ships a `--bind-session` flag / API, swap the
  implementation and delete the SQL — nothing else moves.
- **Why:** Isolates the foreign-schema coupling to one file; makes the eventual
  migration to the clean path a single-component change.

#### Fix 7 — Move the saga out of `session_kv_server` into a dedicated bridge
- **What:** `session_kv_server.py` is a KV store on `main`; PR #333 turned it into an
  orchestrator (shells to `hermes`, writes two DBs, calls the agent API). Extract the
  orchestration into a dedicated bridge module; keep the KV server as HTTP endpoints +
  storage that delegate to it.
- **Why:** Separation of concerns, testability, and it makes Fixes 4–6 land cleanly.

#### Fix 8 — Prefer `--mode shared` for phase 1
- **What:** Run the watcher in shared-session mode so all events map to one stable
  session → one stable routing row, instead of per-incident (a new foreign-table row
  per event).
- **Why:** Fewer writes into the table we don't own = smaller blast radius and less
  churn. Graduate to per-incident later if incident isolation is needed. This is a
  watcher config choice, not a code change here.

---

## Operational caveat (regardless of timeline)

Even for staging: **tell the hermes team we are writing rows into `gateway_routing`
directly, and in what shape.** Right now this coupling is invisible to the team that
owns the table — the day they migrate its schema, our writes break (or break their
assumptions) and it's an incident with no clear owner. A one-line heads-up is cheap
insurance, and it opens the door to the real fix.

## Exit criteria (the fix that deletes all of the above)

Ask hermes for a supported way to open a proactive thread bound to a session — a
`hermes send --bind-session <id>` flag or a small endpoint that sends the alert *and*
registers reply routing atomically, owned by hermes. When that exists, implement it
behind the Fix 6 adapter, delete the direct `gateway_routing` write, and this entire
document becomes obsolete.
