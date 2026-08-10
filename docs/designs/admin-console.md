# Kube Agents Console

**Status:** Local interactive console

## Summary

Kube Agents Console is a first-party web interface for chatting with the
kube-agents front door and explaining human-initiated and autonomous activity
using normalized telemetry and explicit causality.

The console is implemented in `admin_console/`. Setup's Connection page
owns Google Cloud project selection, connection, disconnect, and the diagnostic
checklist. The Observability pages read bounded live Cloud Logging and Cloud
Trace data. Remote authentication and operator-managed deployment remain
follow-up work.

## Product questions

The activity experience must answer:

- What did a user ask and what did the agent reply?
- Which agents, delegated tasks, tools, clusters, and resources were involved?
- What was the outcome, duration, and failure state?
- What did the system do from cron, Kubernetes events, retries, or follow-up
  work without a new human message?
- Is the causal link explicit, inherited, inferred, or missing?

Time proximity is not proof of causality. The UI must label inferred joins and
must not render them as authoritative edges.

## Architecture

```text
Browser
  -> authenticated admin console
       -> Hermes run client
            -> default Chat Agent
            -> run events and approvals
       -> telemetry provider
            -> Cloud Logging
            -> Cloud Trace
            -> session metadata
            -> kanban read model
       -> Task Kanban inspector
            -> board, task, run, event, and delivery projections
```

The production console should run in a separate Deployment with a dedicated
read-only telemetry identity and a narrow, authenticated chat proxy. It must not
receive the Platform Agent API key, operational credentials, or unrestricted
access to the credential proxy. The current local prototype invokes a fixed
client in the selected agent pod and uses only the operator-managed non-secret
loopback trust sentinel.

## Portal API and shared chat abstraction

### Decision

The console will expose an authenticated FastAPI service as its stable product
API. The Streamlit Chat page, evaluation runners, and future operator tooling
will all call that API. Streamlit will no longer call Hermes, query its session
database, or inspect the Kanban database directly.

This is a product integration surface, not a test-only endpoint. An evaluator
is one client of the same contract used by the UI:

```text
Browser                         Evaluation runner
   |                                   |
   +------------ HTTPS/SSE ------------+
                       |
                Portal FastAPI
                       |
                ChatService interface
                 /          |          \
          Hermes runs   session reads   task projection
              API                         / events
```

The API owns the difference between a short front-door run and a complete user
interaction. A Hermes `run.completed` event means that one model turn ended. It
does not mean that downstream Kanban work has finished. The proxy joins the
front-door run with all delegated work and emits its own terminal interaction
event only when the whole interaction is terminal.

This boundary keeps evaluation black-box: the target agent is exercised
through a supported product API, and the evaluator does not require changes to
agent prompts, skills, or tool implementations. The API may expose sanitized
observability that the product already records, but it must not add behavior
that exists only to help a test pass.

### Lessons from the combined FastAPI and Streamlit deployment

The Covariant backend demonstrates a practical single-port composition:
FastAPI owns process lifespan and the public listener, starts Streamlit as a
private subprocess, and reverse-proxies Streamlit HTTP and WebSocket traffic.
The console should reuse that shape with these constraints:

- FastAPI is the parent process and starts and stops Streamlit from its lifespan
  handler. Shutdown first stops new API work, then drains or checkpoints active
  interactions, and finally terminates Streamlit.
- Streamlit binds only to a loopback port. FastAPI owns the externally reachable
  port and proxies page, static, component, and `_stcore` WebSocket traffic.
- API routes are registered before the Streamlit catch-all route. `/api/v1/*`,
  `/healthz`, and `/readyz` can never be forwarded to Streamlit.
- Browser and API traffic remain same-origin. Production CORS is disabled unless
  an explicit client origin is configured.
- FastAPI derives authenticated identity and sends only trusted identity headers
  to Streamlit. Client-supplied identity headers are stripped.
- Readiness reports API readiness separately from Streamlit readiness so a
  broken dashboard does not make an otherwise healthy API appear ready, or the
  reverse.
- Streamlit startup uses bounded retry and a clear `503` response. The proxy
  forwards neither hop-by-hop headers nor upstream cookies outside their
  intended path.

For local development, FastAPI can keep the existing loopback URL while moving
Streamlit to a private secondary port. In-cluster deployment exposes only the
FastAPI Service. Multiple FastAPI replicas require a shared interaction store
and sticky or durable upstream run handling; the first release remains a
single-replica Deployment until those conditions are met.

### Dependency direction and source layout

The refactor will separate domain behavior, upstream adapters, HTTP delivery,
and clients:

```text
admin_console/
  api/
    app.py                 FastAPI construction and lifespan
    auth.py                principal and role dependencies
    models.py              versioned request and response models
    routes/
      agents.py
      interactions.py
      sessions.py
      tasks.py
    streamlit_proxy.py     HTTP and WebSocket reverse proxy
  chat/
    service.py             ChatService orchestration
    completion.py          deterministic aggregate state machine
    store.py               interaction and event store interface
    models.py              internal domain models
  adapters/
    hermes.py              Hermes run API adapter
    runtime.py             bounded session and task projection adapter
    kubectl_local.py       transitional local-only transport
  clients/
    portal_api.py          typed client used by Streamlit
  pages/                   Streamlit pages; API clients only
```

The central interface is `ChatService`, not FastAPI and not Streamlit. It owns:

- starting an interaction;
- observing the root run;
- resolving an allowed approval decision;
- discovering and tracking delegated tasks;
- building a bounded, redacted interaction projection;
- deciding aggregate terminal state; and
- appending durable interaction events.

FastAPI validates and authorizes requests, calls `ChatService`, and serializes
its models. The Streamlit page calls the typed HTTP client. Unit tests substitute
fake upstream adapters beneath `ChatService`; API tests use FastAPI's in-process
test client; UI tests stub `PortalApiClient`. There is one implementation of
completion semantics.

The existing `AgentChatProvider` and `AgentRuntimeProvider` become transitional
adapter inputs. Their orchestration must not be copied into route handlers or
Streamlit pages. Once the API runs in-cluster against narrow upstream services,
the `pods/exec` adapters are removed.

### Resource model

An **agent** identifies the black-box target and profile. A **session** is the
durable conversation. A user message creates an **interaction**. One interaction
contains one root Hermes **run** and zero or more delegated **tasks**. Every
state change is an append-only **event**.

```text
Agent
  -> Session
       -> Interaction
            -> root Run
            -> Approval requests
            -> delegated Tasks
                 -> child Tasks
            -> Messages and results
            -> ordered Events
```

The interaction is the unit that the portal presents and an evaluator scores.
It receives a server-generated `interactionId` at ingress. The ID is propagated
to trusted session metadata, run telemetry, task creation, and audit records
where the runtime supports it. Until propagation is complete, the proxy may use
the portal-owned session plus captured task IDs as an explicitly labeled
fallback; time proximity alone is never authoritative.

Only one interaction may be active in a portal-owned session during the first
release. This prevents a session-only fallback from assigning a delegated task
to the wrong user turn. Concurrent work uses separate sessions.

### HTTP API

The versioned API prefix is `/api/v1`. JSON fields use lower camel case and
timestamps use RFC 3339 UTC. Error bodies have a stable `code`, user-safe
`message`, optional `retryable`, and a request ID. Unknown fields are rejected
on commands and tolerated on reads to allow additive server evolution.

#### Discovery and health

```text
GET  /healthz
GET  /readyz
GET  /api/v1/agents
GET  /api/v1/agents/{agentId}
```

Readiness checks the store and the configured upstream agent boundary without
running a model turn. It never creates, repairs, or grants anything.

#### Start and observe an interaction

```text
POST /api/v1/interactions
GET  /api/v1/interactions/{interactionId}
GET  /api/v1/interactions/{interactionId}/events
POST /api/v1/interactions/{interactionId}/cancel
```

`POST /api/v1/interactions` accepts an idempotency key and returns `202`:

```json
{
  "agentId": "platform-agent",
  "profile": "default",
  "sessionId": "portal_9c7c...",
  "input": { "text": "Design a stockout-resilient GKE cluster." },
  "timeoutSeconds": 1800
}
```

The caller may omit `sessionId` to create a session. The authenticated principal,
not the body, determines user identity. The API rejects an idempotency key that
is reused with a different body.

The response contains stable links rather than leaking the Hermes base URL:

```json
{
  "interactionId": "int_01J...",
  "sessionId": "portal_9c7c...",
  "runId": "run_...",
  "status": "accepted",
  "links": {
    "self": "/api/v1/interactions/int_01J...",
    "events": "/api/v1/interactions/int_01J.../events"
  }
}
```

The event endpoint uses Server-Sent Events. Events have monotonic sequence
numbers per interaction and support `Last-Event-ID`, so a portal refresh,
evaluator restart, or transient disconnect can resume without losing the
terminal event:

```json
{
  "id": "evt_01J...",
  "sequence": 12,
  "type": "task.completed",
  "occurredAt": "2026-08-10T15:04:05Z",
  "interactionId": "int_01J...",
  "sessionId": "portal_9c7c...",
  "runId": "run_...",
  "taskId": "t_...",
  "data": {}
}
```

Stable event types are:

- `interaction.accepted`, `interaction.running`,
  `interaction.waiting_for_approval`, `interaction.delegated`,
  `interaction.completed`, `interaction.failed`, `interaction.cancelled`, and
  `interaction.timed_out`;
- `run.started`, `run.progress`, `run.completed`, `run.failed`, and
  `run.cancelled`;
- `approval.requested` and `approval.resolved`;
- `task.created`, `task.ready`, `task.running`, `task.retrying`,
  `task.completed`, `task.failed`, and `task.cancelled`; and
- `message.created` and `evidence.available`.

Internal framework events can be stored for diagnostics but are not promoted to
the public contract until their meaning and redaction are stable.

#### Approvals

```text
POST /api/v1/interactions/{interactionId}/approvals/{approvalId}
```

The body contains `decision: "approve_once"` or `decision: "deny"`. Permanent,
bulk, and policy-changing approvals are not exposed. The server verifies that
the approval belongs to the interaction, remains pending, and is permitted for
the caller. Repeating the same decision is idempotent; conflicting decisions
return `409`.

An evaluation Persona expresses an approval policy, but the evaluator sends the
exact API decision. It must never ask an LLM to compose protocol text such as
`/approve`; this avoids the approval-interruption failure observed in the older
Google Chat evaluator.

#### Sessions, messages, and delegated work

```text
GET /api/v1/sessions/{sessionId}
GET /api/v1/sessions/{sessionId}/messages
GET /api/v1/sessions/{sessionId}/interactions
GET /api/v1/interactions/{interactionId}/tasks
GET /api/v1/tasks/{taskId}
GET /api/v1/tasks/{taskId}/events
```

All collections are paginated and bounded. Page tokens are opaque. Task
responses expose state, assignee, retry count, latest result, safe error, and
child IDs; they do not expose delivery credentials, attachment filesystem
paths, raw prompts, or unredacted command output.

The API returns root output, task results, and messages as separate fields. It
does not fabricate an assistant message by concatenating task summaries. The UI
may render them together, while an evaluator can assert on each evidence type
without confusing a delegated result with something the front-door agent said.

### Deterministic asynchronous completion

The proxy uses a deterministic aggregate state machine. An LLM may evaluate the
meaning or quality of a final message, but it never decides whether work is
still running.

```text
accepted
  -> running
       -> waiting_for_approval -> running
       -> delegated -----------> running
       -> completed
       -> failed
       -> cancelled
       -> timed_out
```

An interaction is terminal only when all of these are true:

1. The root run is terminal.
2. Every directly or transitively linked delegated task is terminal.
3. No approval belonging to the interaction is pending.
4. All task results discovered before the terminal boundary are durably stored.
5. A bounded settlement check finds no newly linked child task.

`run.completed` with active tasks therefore produces `interaction.delegated`,
not `interaction.completed`. A task that failed and then succeeded on a later
run is completed with retry diagnostics; its historical failure does not
override the final task state. A terminal failed or cancelled task makes the
interaction fail unless the Scenario explicitly treats that branch as optional.
The product API itself does not accept test Scenarios, so the base interaction
status remains conservative: any terminal failed branch is failure.

If upstream state cannot be read, the interaction remains non-terminal until
its deadline. The eventual result distinguishes target failure from
`upstream_unavailable`, `correlation_incomplete`, `event_stream_lost`, and
`proxy_timeout`. Clients must not interpret a proxy or evidence outage as proof
that the agent failed.

The proxy persists its own state before acknowledging a command. On restart it
reconciles non-terminal interactions against the Hermes run endpoint and task
projection, appends any missing derived events, and resumes observation. If the
Hermes process lost a process-local run, the proxy records that explicitly and
continues to reconcile any already-created delegated tasks.

### Shared use by the portal and evaluator

The Streamlit Chat page follows this sequence:

1. Create an interaction through `PortalApiClient`.
2. Render the user message immediately with accepted state.
3. Subscribe to interaction events and reconnect with `Last-Event-ID`.
4. Render approvals as exact approve-once or deny actions.
5. Render linked task state from interaction/task resources.
6. Stop polling or streaming only on a terminal interaction event.
7. Reload the same messages, task results, and terminal state after refresh.

An evaluation runner uses the identical sequence. Its evaluation matrix remains
outside the portal and resolves an Agent, Persona, Scenario, Goals, Assertions,
and repetitions into Runs. The Agent points at the portal API; the Persona owns
the authenticated actor and approval policy; the Scenario supplies messages
and timeouts. Evaluation begins only after the interaction is terminal.

Goal evidence maps to the API as follows:

- **Tool goals** use sanitized run events plus independent audit or external
  state. An agent statement that it used a tool is never evidence by itself.
  If a required tool action is not externally observable, matrix validation
  rejects that goal as unverifiable.
- **Message goals** use the final agent messages and task results, with explicit
  grounding references to tool or external evidence.
- **Soft goals** judge professionalism, clarity, concision, and actionability
  only after required Tool and Message goals have been scored. They cannot make
  a functionally failed interaction pass.

The API is not a replacement for transport-specific testing. A run through the
default profile covers the Chat Agent, routing, delegation, and Platform Agent,
but it does not cover Google Chat publication, Pub/Sub delivery, or Slack socket
delivery. Those remain separate integration Scenarios that use the real surface
and may consume the portal API only for observation and diagnostics.

### Authentication and authorization

FastAPI owns authentication for both the UI and API:

- local UI access continues to use the launcher-verified gcloud identity;
- in-cluster human access uses the configured application or IAP identity;
- automated evaluation uses a dedicated workload identity or short-lived OIDC
  token, never a shared browser cookie;
- the browser and evaluator never receive the Hermes API key or loopback trust
  sentinel; and
- trusted principal fields replace, rather than merge with, client-supplied
  identity headers.

The first roles are `viewer`, `operator`, and `evaluator`. A viewer can read
authorized sessions and activity. An operator can create interactions and make
approve-once or deny decisions. An evaluator can create and observe only its own
evaluation sessions and can make only the decisions allowed by its configured
Persona policy. Every command and sensitive read is audited with principal,
interaction, request, decision, and outcome identifiers.

The FastAPI process needs only the narrow capabilities behind its adapters. The
local transition may retain bounded `pods/exec`, but the production Deployment
must replace it with authenticated Hermes, session, and task services. Broad
Kubernetes access is not an acceptable permanent implementation of the proxy.

### Durability, retention, and redaction

Define an `InteractionStore` interface before choosing storage. Local and
single-replica development can use SQLite in WAL mode on owner-only storage.
Production multi-replica deployment requires a shared transactional database.
The store contains interaction state, an append-only event log, idempotency
records, approval decisions, evidence references, and bounded redacted
projections; it does not become a second unbounded copy of every Hermes log.

Retention is explicit per record class. Deleting an expired interaction also
deletes its stored projections and idempotency record after the replay window,
while retaining only policy-required audit metadata. Redaction happens before
persistence and again before response serialization. Raw secrets, bearer
tokens, credential-proxy payloads, and unrestricted tool output are never
stored in the interaction database.

### Failure and HTTP semantics

- `400`: malformed command or invalid transition.
- `401`: no valid principal.
- `403`: principal lacks access to the resource or action.
- `404`: unknown or intentionally concealed resource.
- `409`: conflicting idempotency key, active interaction conflict, stale
  approval, or already-terminal command.
- `422`: valid JSON that violates the command model.
- `429`: per-principal or per-agent concurrency limit.
- `502`: authenticated upstream rejected or returned an invalid response.
- `503`: required upstream or store is unavailable before work is accepted.
- `504`: synchronous compatibility wait expired; the interaction remains
  queryable unless it reached its own terminal timeout.

Once a request returns `202`, later upstream failures are interaction events and
resource state, not a second HTTP response. Every error identifies whether a
retry is safe. Retrying a start command is safe only with its original
idempotency key.

### Delivery plan

1. **Freeze the domain contract.** Add typed internal models, state transition
   tests, OpenAPI examples, error codes, and golden SSE fixtures. Prove that
   root completion with an active delegated task is non-terminal.
2. **Extract `ChatService`.** Move run, approval, session, and task orchestration
   out of Streamlit. Wrap the current providers as adapters without changing
   their external behavior.
3. **Add the FastAPI process.** Implement health, discovery, interaction reads,
   and a stub-backed interaction command path. Add lifespan-controlled
   Streamlit startup and HTTP/WebSocket reverse proxying.
4. **Make interactions durable.** Add the store, idempotency, ordered events,
   SSE replay, restart reconciliation, deadlines, and bounded settlement.
5. **Connect Hermes and delegated work.** Drive `/v1/runs`, approvals, and stop
   through the Hermes adapter; join recursively linked tasks and results through
   the runtime adapter. Label fallback correlation until `interaction.id` is
   end-to-end trusted.
6. **Move the Chat page.** Replace direct `AgentChatProvider` and
   `AgentRuntimeProvider` calls with `PortalApiClient`. Preserve refresh,
   approval, task-card, and history behavior through the API.
7. **Add the evaluator client.** Implement the evaluation Agent adapter against
   interactions and resumable SSE. Do not put evaluation definitions or judge
   behavior in the portal API.
8. **Deploy the combined service.** Add FastAPI/uvicorn dependencies, one public
   Service port, a private Streamlit port, probes, NetworkPolicy, ServiceAccount,
   configuration, and a single-replica durability volume. Keep local launch
   behavior compatible.
9. **Remove transitional privilege.** Replace local `pods/exec` and direct
   database reads in the production provider with narrow authenticated upstream
   APIs. Reduce RBAC and add authorization, redaction, retention, concurrency,
   disconnect, and restart tests.

### Acceptance criteria

- The Streamlit Chat page and evaluator both use the same published FastAPI
  interaction contract.
- No Streamlit page imports or constructs a Hermes, kubectl, session database,
  or Kanban database client.
- `run.completed` never completes an interaction while linked work is runnable.
- An interrupted SSE client resumes from its last event without duplicating an
  interaction or approval decision.
- A FastAPI restart preserves accepted interactions and reconciles their state.
- Approvals are scoped, exact, idempotent, and limited to approve-once or deny.
- Tool assertions require observed evidence; agent promises do not pass.
- The default profile exercises the same Chat Agent front door used by Google
  Chat and Slack.
- The browser and evaluator never receive upstream agent credentials.
- The combined deployment serves FastAPI and Streamlit through one authenticated
  public port, including Streamlit WebSockets.
- Unit, API, UI, restart, timeout, approval, delegation, and end-to-end tests
  cover both direct and delegated interactions.

### Non-goals

- Reimplementing Hermes inference or Kanban scheduling in the portal.
- Teaching the proxy to infer completion from natural-language phrases.
- Exposing internal SQLite schemas as the public API.
- Making evaluator-specific prompts, goals, or judge models part of the product
  API.
- Treating portal API coverage as proof that Google Chat or Slack ingress works.
- Granting the console broad operational credentials or permanent approval
  authority.

## Correlation contract

The production telemetry provider will normalize source records around:

```text
event.id
event.timestamp
interaction.id
trigger.kind
origin.session.id
origin.user.id
origin.platform
origin.chat.id
origin.thread.id
trace.id
span.id
kanban.task.id
kanban.parent_task.id
agent.name
agent.type
action.type
action.name
action.status
resource.project
resource.cluster
resource.namespace
resource.kind
resource.name
attribution.level
```

`interaction.id` is created by trusted ingress for each human message or
autonomous trigger. Child tasks inherit it. Model-provided values cannot
override identity or correlation fields.

## UI structure

- **Connection:** one editable project selector with source-labeled suggestions,
  project-ID validation, and mutually exclusive Connect and
  Disconnect controls at the top of Setup. Connect auto-selects the one
  GKE cluster labeled `kube-agents-host=true`. Zero or multiple labeled hosts
  produce a red detection error and a separate manual cluster picker whose
  action is Select. Selection is locked while connected. Observability remains
  unavailable until required checks pass. Successful local connections persist
  only validated target metadata in an owner-only server-side file bound to the
  launcher-verified gcloud account. Reopen always revalidates before restoring
  access; an open browser session revalidates every ten minutes. Connect, Select,
  restore, and revalidation checks execute outside Streamlit's render thread so
  navigation and page content remain responsive. A sidebar status component
  observes the background future; only its completed result changes verified
  state. Disconnect deletes the persisted target.
  The same page shows checklist results for CLI authentication, ADC, APIs, GKE,
  agent runtime state, Logging, structured audit events, and Trace.
- **Agentic:** Chat is the interactive surface for working with the agent.
- **Observability:** an always-visible navigation group containing Overview,
  Activity Explorer, Task Kanban, and Scheduled Cron. A shared connection gate
  replaces provider-backed content with concise connection guidance until the
  selected target is verified.
- **Overview:** activity volume, human/autonomous split, attention items,
  attribution coverage, recent outcomes, and page-local activity scope.
- **Chat:** an always-discoverable session workspace with recent Hermes
  sessions in a full-width filterable, selectable, URL-paginated table and a
  URL-selected transcript below it, portal-native composition, read-only
  third-party history, safe portal follow-ups, approve-once or deny controls,
  and inline Kanban progress, retries, and results joined by `session_id`.
  Threads from portal and external surfaces poll only while linked tasks are
  runnable; a small polling indicator updates independently of stable task
  cards, and each task ID links to its Task Kanban detail. A disconnected page
  directs the user to Connection instead of disappearing from navigation.
- **Task Kanban:** a read-only board under Observability with status and assignee
  filters, a selectable URL-paginated task table, selected details below it,
  dependencies, linked chat, delivery state, runs, results, comments,
  attachments, and lifecycle events.
- **Activity Explorer:** filters, aggregate Sankey, per-interaction timeline,
  forensic ledger, and page-local activity scope.
- **Scheduled Cron:** live Hermes job definitions, scheduler heartbeat state,
  active and recent executions, manual-versus-scheduled trigger evidence, and a
  UTC calendar of recent runs and projected cron or interval occurrences for
  the next 21 days. High-frequency schedules are summarized per job and day.
  An enabled job without a live profile ticker is explicitly reported as
  unable to run automatically.

## Current data readiness

The local UI reads operational activity from Cloud Logging and Cloud Trace.
The provider is bounded by an explicit project, optional cluster, time window,
record limits, and the launcher-verified identities. Unsupported correlations
remain labeled as missing instead of being filled with demo data.

| Surface or data                         | Current state                                                                                            | Dummy? | Real source exists? | Portal connector exists? | Production solution                                                                                     |
| --------------------------------------- | -------------------------------------------------------------------------------------------------------- | ------ | ------------------- | ------------------------ | ------------------------------------------------------------------------------------------------------- |
| Signed-in identity                      | Active gcloud account passed by the local launcher                                                       | No     | Yes                 | Yes                      | Keep launcher verification for local use; use application-level authentication for remote deployment    |
| Connection diagnostics                  | Bounded live checks for project, APIs, GKE, logs, audit, and Trace                                       | No     | Yes                 | Yes                      | Reuse the proven source adapters in the production telemetry provider                                   |
| Overview metrics                        | Aggregated from the selected live Logging and Trace snapshot                                             | No     | Yes                 | Yes                      | Add server-side aggregation for windows larger than the bounded snapshot                                |
| Activity pulse                          | Live evidence records grouped into 15-minute buckets                                                     | No     | Yes                 | Yes                      | Add paginated historical aggregation                                                                    |
| Human versus autonomous classification  | Trusted session and cron evidence; unsupported records remain unknown                                    | No     | Partial             | Yes                      | Create a trusted interaction and trigger record at chat, cron, event, retry, and follow-up ingress      |
| Attribution coverage                    | Computed from explicit, inherited, and missing live identifiers                                          | No     | Partial             | Yes                      | Propagate trusted interaction identity through every action                                             |
| Recent work and active-agent summaries  | Aggregations over normalized live events                                                                 | No     | Yes                 | Yes                      | Join the Kanban task read model                                                                         |
| Activity filters                        | URL-persisted project, cluster, and time; local event filters                                            | No     | Yes                 | Yes                      | Persist all investigation filters in the URL                                                            |
| Causal-flow Sankey                      | Only explicit and inherited records; missing/inferred excluded                                           | No     | Partial             | Yes                      | Add first-class gaps and separately styled inferred joins                                               |
| Interaction timeline                    | Uses trusted `interaction.id` when present, otherwise Trace ID                                           | No     | Partial             | Yes                      | Generate and propagate `interaction.id` at every trusted ingress                                        |
| Forensic ledger and event details       | Live normalized evidence with source IDs and Console deep links                                          | No     | Yes                 | Yes                      | Add controlled evidence export                                                                          |
| User prompts and agent responses        | Access-controlled Trace evidence, portal-redacted and size-bounded                                       | No     | Yes                 | Yes                      | Add policy-aware reveal auditing and upstream DLP scrubbing                                             |
| Persisted session history               | Bounded reads from every Hermes profile in the selected agent                                            | No     | Yes                 | Yes                      | Add a dedicated read-only API and per-message participant attribution                                   |
| Interactive portal chat                 | Hermes run API through a bounded fixed in-pod client                                                     | No     | Yes                 | Yes                      | Add a narrow operator-managed chat proxy and durable run/event state                                    |
| Specialist progress in portal threads   | Bounded Kanban tasks and latest run result joined by Hermes session ID                                   | No     | Yes                 | Yes                      | Expose this projection through the narrow chat API instead of `pods/exec`                               |
| Task Kanban task inspection             | Bounded live board, detail, run, event, relationship, and delivery reads                                 | No     | Yes                 | Yes                      | Add a dedicated policy-aware Kanban read API with pagination                                            |
| Agent tool calls                        | Live Trace spans and structured tool audit records                                                       | No     | Yes                 | Yes                      | Add complete trusted user and task lineage                                                              |
| Kubernetes resources and mutations      | Trace labels and tool targets only; no API-server audit connector                                        | No     | Yes                 | Partial                  | Ingest API-server audit logs and join using trusted request IDs                                         |
| Credential-proxy activity               | Not ingested; no proxy events are fabricated                                                             | N/A    | Yes                 | No                       | Normalize proxy request ID, policy decision, command digest, execution state, exit code, and duration   |
| Approval activity                       | Live Trace spans and structured request/response audit records                                           | No     | Yes                 | Yes                      | Correlate requester, approver, policy, command digest, and interaction ID                               |
| Scheduled Cron page                     | Bounded live Hermes job stores, execution databases, profile ticker health, and recent/upcoming calendar | No     | Yes                 | Yes                      | Add a dedicated policy-aware scheduler API and retained execution pagination                            |
| Time window and retention state         | URL-persisted bounded windows and Trace page budget with source errors and truncation shown              | No     | Yes                 | Yes                      | Add retention and sampling metadata from source configuration                                           |
| Refreshable and shareable investigation | Project, cluster, and window persist; local filters do not                                               | N/A    | N/A                 | Partial                  | Persist filters, interaction, and selected evidence in URL query parameters                             |
| Saved case or evidence export           | Not implemented                                                                                          | N/A    | N/A                 | No                       | Export access-controlled evidence bundles with source IDs, query scope, redaction state, and timestamps |

### Dependencies and blockers

Nothing blocks continued UI development or implementation of read-only,
single-source views. The following gaps block the portal from making reliable
end-to-end causal claims:

1. There is no trusted `interaction.id` shared by chat ingress, delegated tasks,
   tool calls, approvals, credential-proxy requests, and autonomous triggers.
2. Tool and approval audit records do not consistently contain user, session,
   trace, agent, interaction, and parent-task identity.
3. Credential-proxy request IDs are not connected to the initiating tool,
   interaction, trace, or requester.
4. Kubernetes audit records authoritatively identify the workload
   ServiceAccount, but the human-request join is currently based on time and
   target unless trusted request metadata is present.
5. The activity provider does not yet read credential-proxy or Kubernetes
   API-server audit sources. Chat has a bounded session-to-Kanban projection,
   but it is not a general activity-source join.
6. Remote deployment still requires application-level authentication,
   authorization, redaction policy, retention policy, and audited sensitive-data
   access. The loopback-only gcloud launcher is not a remote authentication
   design.
7. Hermes currently stores requester identity at session level, not per message.
   Shared threads must remain visibly unattributed until ingress persists a
   participant record keyed by platform message ID.
8. API-run state and approval queues are process-local. Gateway restart or
   non-sticky multi-replica routing can lose an active run transport.
9. Downstream Kanban workers have distinct execution state from the front-door
   run. Non-YOLO deployments need task-to-worker approval correlation before a
   front-door UI can approve specialist actions reliably.
10. Google Chat and Slack provide durable adapter destinations for Kanban
    notifications. The current Hermes API client is represented as an
    ephemeral TUI `run_*` subscription, which the gateway notifier cannot
    deliver to after the request stream closes. The portal therefore polls the
    task read model by trusted session ID; a production chat API should expose
    a durable task-event stream instead.

The production provider can be built incrementally before all correlation work
is complete. It must label unavailable, missing, and inferred joins and must not
present time-adjacent records as proven causality.

## Delivery iterations

1. **UI prototype:** domain interfaces, demo data, visual system, activity
   views, and connection onboarding.
2. **Telemetry foundation:** propagate interaction, trigger, session, trace,
   and kanban identifiers into structured audit records.
3. **Cloud provider:** bounded Cloud Logging and Cloud Trace queries,
   normalized joins, redaction, completeness state, and Console deep links.
4. **Operator integration:** separate Deployment, Service, ServiceAccount,
   optional IAP/Gateway exposure, and CRD configuration.
5. **Hardening:** authorization roles, sensitive-value reveal auditing,
   retention, load tests, query quotas, and end-to-end staging tests.

## Current boundaries

- Activity is live and read-only; no demo events are used by application pages.
- Logging and Trace reads are capped. Reaching a cap is shown as incomplete;
  continuation-token pagination is not yet implemented.
- Common credential forms are redacted before evidence is rendered, but this
  is not a replacement for upstream secret and PII scrubbing.
- Trusted interaction identity is not present on every source record. The UI
  does not create time-adjacent causal joins.
- The Connection page performs bounded, read-only gcloud and Cloud Trace
  requests. It does not change APIs, IAM, or Kubernetes resources.
- The local persisted connection is not an authentication token. It contains
  account and deployment coordinates plus verification time, but no Google
  credential, chat content, or telemetry. Google Cloud CLI remains the
  credential owner and mints tokens on demand. A remotely served console should
  instead use a server-side session store with an opaque, Secure, HttpOnly,
  SameSite cookie and explicit session expiry/revocation.
- No credential-proxy endpoint is called.
- Interactive Chat uses `pods/exec` to run a fixed client which calls the
  loopback Hermes API. Prompts travel over stdin, inputs and outputs are bounded,
  and the external API key is never read. Production requires a narrow chat API
  instead of `pods/exec`.
- The local client attributes only `portal_*` sessions to the launcher-verified
  gcloud account with source `admin_portal`. Google Chat and Slack sessions stay
  read-only. A production API must authenticate the caller and own this metadata
  write directly.
- Chat History currently uses a fixed read-only query through `pods/exec`.
  Production deployment requires a dedicated read-only history API so the
  console does not need the broader exec permission.
- Task Kanban inspection uses the same fixed in-pod read mechanism. Queries are
  capped at 500 tasks, 100 runs, 500 events, 200 comments, and 100 attachments;
  delivery destinations and attachment storage paths are deliberately omitted.
  Production requires a paginated, policy-aware Kanban read API.
- The interfaces in `admin_console/domain.py` are the intended seams for the
  production providers.
