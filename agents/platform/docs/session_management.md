# Platform Session Management & Incident Triage Flow

This document details the architecture and workflow for routing GKE Kubernetes warning alerts into persistent diagnostic agent sessions, enabling interactive threaded troubleshooting in chat platforms (Google Chat and Slack).

---

## Architecture Overview

AI agent execution is typically stateless and triggered on-demand. To support proactive GKE warning troubleshooting, we run a local stateful proxy server called `session_kv_server.py` (the REST Bridge) on the Platform Agent host on `127.0.0.1:8699`.

This server acts as a bridge between the **GKE Event Watcher** (monitoring target clusters) and the **Hermes Gateway** (running the LLM reasoning turns). The turn it starts runs on the gateway's default profile — the **Planning Agent** — which delegates the diagnosis on the kanban board to the **Cluster Agent** of the cluster that raised the event, so the agent that investigates the failure is the one scoped to the cluster it happened on.

It binds loopback rather than `0.0.0.0` because it has exactly four callers and all of them share this Pod's network namespace: the event watcher in the credential-proxy container, the Platform MCP server, the `incident_context` plugin, and the gateway's kanban notifier. Every route except `/healthz` also requires a bearer token from the `SESSION_KV_API_KEY` key of the agent's Secret — the rows it serves carry chat identifiers, and loopback inside a shared namespace is not on its own an authorization boundary. Deliberately not `API_SERVER_KEY`, which is the non-secret sentinel `cluster-internal-trusted` and would authenticate nothing. When the key is absent the server answers `503` to every authenticated route and logs why; see [the credential-isolation design](../../../docs/credential-isolation-design.md#the-loopback-only-exception).

### Key Responsibilities:

1. **Deduplication:** Maps repeat events to the same troubleshooting session, preventing alert flooding and saving LLM token costs.
2. **Dynamic Thread Resolution:** Captures the Chat API message ID returned from the first alert, saving it as the persistent thread key.
3. **Incident Triage Context Preservation:** Persists completed triage reports inside the local SQLite database.
4. **Gateway Message Rewriting Hook:** Integrates the `incident_context` plugin to intercept user replies on active incident threads and automatically prepend the triage report, allowing the fixer agent session to run with full context.
5. **Severity Gate & Event Ledger:** Records every forwarded event in `intercepted_events`, then alerts on the warning ones only. Informational events are held back from chat and reported as a count by the daily recap.
6. **Daily Alert Ceiling:** Caps how many alerts of each severity reach chat in one UTC day, bounding the volume that survives deduplication.
7. **Scheduled-Report Relay:** Accepts a finished report from a specialist's cron job on `POST /v1/cron-reports` and gives the Chat Agent one turn to present it, so a scheduled finding lands in a thread the Chat Agent can answer follow-up questions about. Its caller is the scheduler, not the model: `deliver: "chat"` resolves to a delivery-only platform plugin whose sender POSTs here, and `report_to_chat` remains for a job that needs to report mid-run. Deliberately not a mode of `/sessions/{id}/inject`: a scheduled report has no severity and must not spend the alert ceiling above. See [the design](../../../docs/designs/cron-report-relay.md).
8. **Triage Routing:** Instructs the front door to hand the diagnosis to the Cluster Agent of the cluster the event came from, and records the chat route that carries the report back.

### Triage Routing

The session lands on the front door and cannot land anywhere else. Hermes selects a profile by URL prefix (`POST /p/<profile>/api/sessions`), only when `gateway.multiplex_profiles` is enabled — it is off by default and this install does not set it — and only against that profile's own `API_SERVER_KEY`. A `profile` key in the request body is accepted with a `201` and dropped, so it looks like routing and is not. Routing is therefore a prompt, not a parameter.

`_build_agent_query` writes that prompt for the front door, and it is addressed to a router rather than to a diagnostician: make exactly one `kanban_create` call, assign it to the `cluster-*` agent scoped to the event's cluster, and copy the body between two markers verbatim. `_triage_task_body` builds that body — the event details and the report template — and it is the front door's job to move it across unread. Everything in that design is a response to the front door being helpful: given the brief as instructions rather than as cargo, it summarised, and filed extra cards asking other agents to deliver the report.

Delivery is the card itself. Hermes subscribes every card to the session it was filed from, and posts a subscribed card's `result` to chat when it turns terminal — so the Cluster Agent finishes with `kanban_complete` and nothing else, and the report reaches the thread the alert was raised in. The body's whole job on that point is to insist the entire report goes in `result`, since `result` is verbatim what the reader sees.

The address is what used to be wrong. An event-triage turn arrives over the REST gateway, whose single ingress chokepoint stamps `platform="api_server"` and puts the session id in `chat_id`, so the subscription was written well-formed and pointing at no chat platform that exists — the report was produced, stored on the card, and delivered nowhere. That is issue #630. `deploy/docker/patches/kanban_event_routing.py` closes it inside the image: it looks the session id up in `session_metadata`, which `_register_session_routing` wrote before the turn started, and substitutes the alert's real `platform`/`chat_id`/`thread_id` at subscription time. A session with no recorded route, or one recorded as non-chat, falls through unchanged.

The session id is therefore the thread key, and `session_metadata` records the `platform` alongside the `thread_id`: a thread belongs to exactly one chat platform, and a report addressed to the other is not degraded to the home channel but refused outright.

### Daily Alert Ceiling

Deduplication bounds how often _one_ failure is reported. It does nothing about many _distinct_ failures at once — a node draining or a namespace collapsing produces a hundred unrelated pods, each a legitimately new incident. The ceiling is the backstop for that case.

`inject_message` classifies severity (`get_severity_details`), applies the [severity gate](#severity-gate), and then spends one of that severity's daily allowance before anything is posted or any agent turn is started. This is the only place both actions pass through, and severity is not known any earlier — `POST /sessions` carries no payload.

| Severity   | Env var                      | Default |
| ---------- | ---------------------------- | ------- |
| `Critical` | `ALERT_DAILY_LIMIT_CRITICAL` | `10`    |
| `Warning`  | `ALERT_DAILY_LIMIT_WARNING`  | `5`     |
| `Info`     | `ALERT_DAILY_LIMIT_INFO`     | `5`     |

`Info` events do arrive — nothing on the path from the kubelet to `inject_message` filters on `Event.Type`, and `BackOff` is on the watcher's default reason list emitted as `type: Normal` for image-pull back-off — but none of them ever bills this bucket, because the [severity gate](#severity-gate) drops every `Info` event before the claim. The `Info` row is kept regardless: deleting it would turn the entry into a `.get(severity, 0)` miss, and `_claim_alert_quota` treats that miss exactly as it treats a limit of `0` — allowed through, uncapped. Narrowing that gate afterwards would therefore send an unbounded `Info` stream to chat rather than restore a ceiling. Setting a limit to `0` turns that severity's cap off entirely, by the same branch.

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
- **The suppressed alert is still acknowledged** to the watcher with `200 {"status": "suppressed"}`, rather than an error code. A 4xx or 5xx would land in `k8s_event_watcher_inject_errors_total`, which exists to say the daemon is broken; refusing an alert over a configured ceiling is it working. The watcher reads the body, drops its dedup entry and re-offers the workload on its next sighting — deliberately, because the entry's window is 24h and this ceiling resets at 00:00 UTC, so keeping it would mute the workload long after the reason for it expired. The price is a session row per re-offer until the day rolls over.
- **The budget survives restarts,** because it is on the `system-metadata` PVC rather than in memory. A crash-looping session server would otherwise hand out a fresh day's quota on every restart, which is precisely the condition the cap exists for.
- **The day boundary is UTC midnight,** not the operator's local midnight.
- **The severity gate runs first, and only alerts that survive it are billed.** A budget is a count of alerts sent, so an event that was never going to be posted must not spend one. Claiming first would bill the `Info` bucket for every suppressed image-pull `BackOff` and leave `GET /v1/alert-quota` reporting a day's worth of alerts nobody received. It cannot starve a real alert of its budget — anything graded `Warning` or `Critical` draws on a different bucket from the `Info` churn — so this ordering is bookkeeping, not a safety property.

---

## End-to-End Workflow

The diagram below details the lifecycles of alert ingestion, session routing, and interactive GitOps fixes:

> **What joins Phase 1 to Phase 3 is the `incidents` row**, and the kanban notifier writes it in the
> same step that posts the report — the only point where the substituted chat address and the card's
> result are both in hand. Without it `_lookup` finds nothing, the reply passes through unrewritten, and
> the front door gets a bare `apply`; that was `main` between #738, which replaced the egress call that
> used to write the row, and #802, which put the write on the delivery path.

```mermaid
sequenceDiagram
    autonumber
    participant K8s as GKE Target Cluster
    participant Watcher as k8s-event-watcher
    participant Proxy as session_kv_server (Port 8699)
    participant Gateway as Hermes Gateway (Port 8642)
    participant Front as Planning Agent (default profile)
    participant Agent as Cluster Agent for the event's cluster
    participant Fixer as Platform Agent (holds the GitOps write path)
    participant Notifier as Kanban notifier
    participant Chat as Google Chat / Slack
    participant Plugin as incident_context Plugin

    Note over K8s, Watcher: Phase 1: Alert Detection & Initialization
    K8s->>Watcher: Pod Eviction Warning (PDB Violation)
    Watcher->>Proxy: POST /sessions (Creates session ID: k8s-evt-abc123)
    Proxy-->>Watcher: Returns sessionID: k8s-evt-abc123
    Watcher->>Proxy: POST /sessions/k8s-evt-abc123/inject (Payload: Event details)
    Proxy->>Proxy: Spend one of today's alerts for this severity (silently drops if the ceiling is reached)
    Note over Proxy: Record the event in db (intercepted_events table, notified = 1)
    Proxy->>Chat: Post the alert (no diagnosis yet)
    Proxy->>Proxy: Record the alert's platform/chat_id/thread_id under k8s-evt-abc123
    Proxy->>Gateway: POST /api/sessions (session k8s-evt-abc123; lands on the default profile)
    Proxy->>Gateway: POST /api/sessions/k8s-evt-abc123/chat (Route this triage)
    Gateway->>Front: Wake up the front door
    Front->>Agent: kanban_create(assignee=cluster-proj-x-loc, body=the brief, verbatim)
    Note over Front, Agent: The card is subscribed to the alert's thread, not to the api_server origin
    Agent->>Agent: Diagnose (read-only), write the report
    Agent->>Agent: kanban_complete(result=the full report)
    Notifier->>Chat: Post the card's result under the alert's thread
    Notifier->>Proxy: POST /v1/incidents (key the report to that thread, so a reply can be resolved against it)

    Note over K8s, Proxy: Phase 1b: Informational Events Stop Here
    K8s->>Watcher: Normal-type event with a watched Reason (e.g. image-pull BackOff)
    Watcher->>Proxy: POST /sessions/k8s-evt-def456/inject (Payload: Event details)
    Note over Proxy: Record with notified = 0, return {"status": "filtered"}
    Note over Proxy, Chat: No alert, no triage session — counted in the daily recap
    Note over Watcher: Keeps its dedup entry: "filtered" is a policy grade, not a ceiling

    Note over K8s, Watcher: Phase 2: Event Deduplication
    K8s->>Watcher: (Duplicate Warning Event occurs)
    Watcher->>Watcher: Detects active session cache for key
    Watcher->>Proxy: POST /sessions/k8s-evt-abc123/inject (Payload: count=5)
    Proxy->>Chat: Post threaded repeat warning message

    Note over Fixer, Chat: Phase 3: Reporting & Human-in-the-Loop Resolution
    Chat->>Plugin: User replies: "apply" (recommended) or "apply Option B" (Hook: pre_gateway_dispatch)
    Plugin->>Proxy: GET /v1/incidents/by-thread
    Proxy-->>Plugin: Return triage report content
    Note over Plugin: Rewrite message text to prepend triage report context
    Plugin->>Gateway: Dispatch the rewritten message
    Gateway->>Front: Ordinary chat ingress, so it lands on the front door
    Front->>Fixer: Delegate the apply — the Cluster Agent that wrote the report cannot open a PR
    Fixer->>Fixer: Create branch, edit git manifests, open GitOps PR
    Fixer->>Chat: Post threaded reply "Created PR #334"
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
  chat_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  report TEXT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (chat_id, thread_id)
);
```

#### `intercepted_events`

One row per event the watcher forwards, whether or not it was announced in chat. `notified` is what
lets the `eod-event-watcher-daily-report` cron job report suppressed informational events as a
number instead of losing them; the watcher's own dedup snapshot cannot substitute, because it is a
rolling window of _active_ incidents keyed by `(uid, reason)`, carries no namespace or workload
name, and resets each entry's `count` when its window rolls over.

`notified = 0` covers three unrelated outcomes and the recap must not conflate them. The [severity
gate](#severity-gate) holds back events Kubernetes itself graded informational, and those are the
recap's subject — reported as a count. An alert the [daily ceiling](#daily-alert-ceiling) dropped
carries the same flag but was graded `Critical` or `Warning` and was on its way to chat, so the
recap reads `severity` to tell the two apart. It does not report those: they are outside its scope,
and counting them as informational would inflate the one number it exists to publish. It does count
them privately, because a day that withheld alerts may not be reported as a clean one — the SOP's
"What this recap does not report" owns that contract.

The third is `delivery_error`, and it exists because `notified` is an _intent_ at the moment it is
written. The row goes in before the chat post is attempted: the send runs in a background task, so a
row written afterwards would be lost outright if the process died mid-flight. A send that then fails
comes back and clears the flag, recording why in `delivery_error` — without that correction the row
reads as delivered, and the recap prints its ✅ all-clear over a day an alert never reached anyone.
It is a separate column rather than a fourth `severity` reading because a ceiling drop and a failed
send prescribe opposite remedies: raise the ceiling, or fix the chat credentials. The recap keeps
that distinction in its counts and drops it from its listing: it names neither class, and prints a
separate ⚠️ total for each above the body. No metric counts the delivery-failure class at all, so
that line and this column are together the only record that a delivery failed anywhere in the
system. The SOP's "What this recap does not report" owns the naming-versus-counting line and says
what to query for the detail. Like `cluster`, it postdates the first draft of this table, and the recap selects it only
when `PRAGMA table_info` says it is there — see "A pre-release table, and no migration" below for
why that tolerance is in the reader and not in `init_db`, and why `cluster` does not get it.

It differs from the ceiling in one further respect. `inject_message` returns on a ceiling refusal
before it queues the background task, so no session is ever created. `trigger_agent_troubleshooter`
marks the delivery failure and then continues into `_create_gateway_session` and `_start_agent_turn`,
so a triage session **does** run for every undelivered alert — it only skips
`_register_session_routing` on that branch, leaving the turn with no thread to reply into even
though `_build_agent_query` instructs the model to thread its findings. The turn still writes its
`incidents` row; it just reaches nobody through chat. Nothing announces that session, so the session
log is where it surfaces.

`cluster` is recorded because one session KV database backs every cluster profile in the pod, the
same reason the ceiling is fleet-wide. Without it the recap cannot tell two same-named workloads in
two clusters apart, and it is what lets the recap's header say which clusters a count covers. It
postdates the other columns, and unlike `delivery_error` the reader gets no tolerance for its
absence: `record_intercepted_event` names it in every INSERT, so a table without it records nothing
at all, and the recap reports that as an unreadable ledger rather than a quiet day — see
"A pre-release table, and no migration" below.

`object_uid` is recorded, and named in the same INSERT under the same terms, because `workload`
cannot substitute for it: `clean_workload_name` strips the replica suffix before the row is written,
so every pod of one Deployment shares a `workload`, a `namespace` and a `reason`. The recap counts
the alerts the daily ceiling withheld, and those two cases are indistinguishable without it — one
pod re-offered all afternoon writes many rows for one lost alert, while forty replicas failing at
once write rows that look the same and are forty. It is the watcher's own dedup key
(`involvedObject.uid`), which the payload has always carried; the daemon simply did not store it.
A payload without one records `''`, since this pod cannot guess another pod's UID. Rows expire on the same 14-day TTL as the rest of the
database, and on a row cap besides — see "Two bounds, not one":

```sql
CREATE TABLE intercepted_events(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  cluster     TEXT NOT NULL DEFAULT '',
  namespace   TEXT NOT NULL DEFAULT '',
  workload    TEXT NOT NULL DEFAULT '',
  object_uid  TEXT NOT NULL DEFAULT '',  -- the involved object's UID; `workload` has its replica suffix stripped
  object_kind TEXT NOT NULL DEFAULT '',
  reason      TEXT NOT NULL DEFAULT '',
  message     TEXT NOT NULL DEFAULT '',
  severity    TEXT NOT NULL DEFAULT '',
  occurrences INTEGER NOT NULL DEFAULT 1,
  notified    INTEGER NOT NULL DEFAULT 0,  -- 0 when the gate, the ceiling or a failed send held it back
  delivery_error TEXT NOT NULL DEFAULT '',  -- non-empty when the chat post failed after notified = 1
  created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

##### Two bounds, not one

This is the only table here whose write rate the cluster sets rather than an operator, so the TTL
does not bound the file. Once the day's ceiling for a severity is spent the daemon answers
`suppressed`, and on that answer the watcher rolls its dedup entry back — so a hundred pods failing
at kubelet's repeat cadence write a row per _sighting_ rather than a row per incident, all day, for
fourteen days. The same database carries thread routing and triage context on the shared session
PVC, so the ledger filling the volume takes those down with it.

`cleanup_old_records` therefore trims the ledger to the newest `SESSION_KV_LEDGER_MAX_ROWS` rows
(200,000) after the TTL delete, and `record_intercepted_event` truncates `message` to
`SESSION_KV_LEDGER_MESSAGE_MAX_CHARS` (512) on the way in. At those bounds the table holds on the
order of a hundred megabytes. The recap's own 120-character cut is a display choice applied at render
time and bounds nothing on disk; 512 keeps more than it shows, which leaves room for a
`FailedScheduling` message that names a predicate per node.

##### A pre-release table, and no migration

`init_db` runs `CREATE TABLE IF NOT EXISTS` and nothing else: there is no `ALTER TABLE` for
`cluster`, `object_uid` or `delivery_error` anywhere in the tree. The table has never been in a release, so the
only databases carrying an earlier shape are dev installs that ran an intermediate commit of the
change that introduced it, and a migration maintained for a shape no user has is machinery that
outlives its reason.

**If you have such an install, you must drop the table before rolling the image.** `CREATE TABLE IF
NOT EXISTS` is a no-op against it, so the columns never appear, and how badly that ends depends on
which column is missing:

- **No `cluster`** — the ledger stops recording anything. `record_intercepted_event` names the
  column in its INSERT, so every write raises `no such column: cluster` into the blanket
  `except Exception` around it, is logged once per event, and is dropped. The table stays empty for
  as long as the shape lasts.
- **No `object_uid`** — the same, and for the same reason: it is in the INSERT, so nothing is
  recorded.
- **No `delivery_error`** — events are still recorded correctly and only the correction fails.
  `mark_delivery_failed` raises `no such column: delivery_error`, the same blanket `except`
  swallows it, and the row keeps `notified = 1`, so the recap counts an alert nobody received as
  one that reached chat.

```bash
sqlite3 "$SESSION_KV_DB_PATH" 'DROP TABLE IF EXISTS intercepted_events;'
```

`init_db` recreates it on the next start. The cost is at most a day of recap data, since the recap
reads a 24-hour window (72 on a Monday) — and on the `cluster`-missing shape there is no data to
lose, because none was ever written.

The reader treats the two shapes the way the writer does. A ledger with no `delivery_error` reads
normally, with the column substituted as empty: that keeps a recap running against a ledger written
by an older _session server_ in the same pod during a rollout, which is a skew a drop cannot fix. A
ledger with no `cluster` or no `object_uid` is a **read failure** — the recap prints the 🔴 card
naming the path
instead of a green all-clear over a table nothing can write to. That card is the only warning this
condition produces; the per-event log lines go to the session server's own stderr, which nobody
reads on a day the recap said everything was fine.

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

## Severity Gate

`get_severity_details` classifies an injected event from its Kubernetes `Event.Type` and `Reason`:
a `Warning` whose reason blocks drain, eviction, or scheduling is `Critical`, any other `Warning` is
`Warning`, and everything else is `Info`. The watcher filters on `Reason` alone and never on
`Event.Type` (`k8s-operator/cmd/k8s-event-watcher/filter.go`), so `Normal`-type events do arrive
here — image-pull `BackOff` being the routine one.

`Info` events are recorded and then dropped: no chat post, no troubleshooter session, and `inject`
returns HTTP 200 with `{"status": "filtered"}`. The 200 is deliberate — a non-2xx counts as an
`injectErrors` failure, so returning one would make a suppressed event look like a broken bridge on
the watcher's metrics.

The word in the body is deliberate too, and is not the `"suppressed"` the daily ceiling answers with
above. The watcher discriminates on that string and nothing else: on `"suppressed"` it drops its
dedup entry so the workload is re-offered, which is right for a ceiling that resets at 00:00 UTC and
wrong here. An `Info` grade is a property of the event, not of the day — the next sighting would
grade the same way — so sharing the word would re-offer every quiet workload at its own repeat
cadence, spending a session, an inject and a ledger row per sighting on an alert nobody was ever
going to receive. On `"filtered"` the watcher keeps the entry and counts
`k8s_event_watcher_events_policy_filtered_total`.

The two halves of that exchange ship on different images — the gate in `session_kv_server.py` on the
agent image, the `"filtered"` handling in a sidecar binary — so either can be older than the other,
and the skew is not equally safe in both directions. An older daemon says `"suppressed"` for both,
which reopens: one redundant session, no silence. An older watcher is the harmful direction, because
it reads the unknown status as delivered, keeps the entry, and has no `MarkPolicyFiltered` to flag
it with — so the re-open described in the next paragraph can never fire and the family's `Warning`
members stay deduped behind the entry for as long as they keep arriving. The daemon therefore does
not answer `"filtered"` unless the caller asked for it: the watcher sends
`X-Watcher-Features: policy-filtered`, and a request without that token gets `"suppressed"` and the
reopen-on-next-sighting behaviour an older watcher already handles correctly.
`k8s-operator/cmd/k8s-event-watcher/README.md` owns that contract in full. The pair is pinned by
`test_the_gate_and_the_ceiling_do_not_answer_with_the_same_word`,
`test_a_watcher_that_cannot_handle_filtered_is_not_sent_it` and by
`TestDispatcherKeepsDedupOnPolicyFilter`.

Keeping the entry needs one qualification, because the entry is not keyed on the event that was
graded. The watcher's key is `(uid, canonical reason)`, and `canonicalizeReason` folds a whole
failure family onto one of them: kubelet's `Normal`-type `BackOff` ("Back-off pulling image"), the
`ErrImagePull` beside it, and the `Warning`-type `Failed` that follows are one incident. A bad image
tag can therefore put the routine member in front, and every `Warning` behind it would be deduped
against an entry held on behalf of an event nobody was told about — permanently, since each sighting
slides the window forward. So the watcher lets the first event the daemon would post re-open the
incident, once per window, counted in `k8s_event_watcher_events_policy_reopened_total` and pinned by
`TestDispatcherReopensPolicyFilteredKeyForWarning`. That is `Warning`, or an empty `Type` — the
endpoint coerces with `payload.get("type") or "Warning"` before grading — and nothing else: a type
the daemon would grade `Info` is refused the reopen rather than spending the family's only one on an
event that comes straight back `filtered`. The watcher README owns that rule in full. That re-opened
event arrives with `count = 1` rather than the family's accumulated count, so `occurrences` keeps
meaning "sightings this row stands for" — the invariant the recap relies on when it sums the column
into "Forwarded _N_ events" and ranks its incident list by it.
`TestDispatcherReopenedPayloadCountsFromOne` pins that number.

The gate is a plain `severity == "Info"` test, and `get_severity_details` reaches that label on
`Event.Type` alone: `Warning` grades `Critical` or `Warning` depending on whether the reason matches
a blocker keyword, and everything else grades `Info`. There is no reason-based exception, which
means a `Normal`-typed event is filtered however serious its reason sounds.

That is a real limit rather than an oversight, and it sits one layer up. Kubernetes types some
failures `Normal` — kubelet records node readiness transitions with
`recordNodeStatusEvent(v1.EventTypeNormal, events.NodeNotReady)`, and the node-lifecycle controller
records the same transition the same way — so a `NodeNotReady` reaching the daemon would be graded
`Info` and filtered. None does: the `--reason` list the operator passes in
deploy/shared/start-services.sh does not forward `NodeNotReady`, and neither the daemon nor a second
copy of a reason list is the place to fix that. `deploy/shared/start-services.sh` decides what the
daemon sees at all, and widening it is what would make a node loss reportable.
`test_the_event_type_is_the_only_thing_that_lifts_an_event_above_info` pins the grading rule so it
is not re-derived by accident.

The label also decides which daily ceiling the alert draws on, so a `Warning`-typed node loss is
billed to `ALERT_DAILY_LIMIT_WARNING` rather than to the informational bucket.

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

To view currently registered incident triage reports. Through Python rather than
the `sqlite3` CLI, which the sandbox image does not ship — the interpreter that
runs the server is the one tool guaranteed to be able to read its database:

```bash
kubectl -n kubeagents-system exec deployment/platform-agent-gateway -c platform-agent -- \
  python3 -c "
import sqlite3
c = sqlite3.connect('/var/lib/kube-agents/session/session_kv.db')
for r in c.execute('SELECT chat_id, thread_id, created_at FROM incidents ORDER BY created_at DESC'):
    print(r)
"
```

### Verify Inbound Plugin Activity

Filter container logs to trace whether the `incident_context` plugin is successfully intercepting threads and rewriting messages:

```bash
kubectl -n kubeagents-system logs deployment/platform-agent-gateway -c platform-agent | grep -E "incident_context|inbound message"
```
