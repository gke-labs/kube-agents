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

- **Connection:** project selection plus mutually exclusive Connect and
  Disconnect controls at the top of Setup. Connect auto-selects the one
  GKE cluster labeled `kube-agents-host=true`. Zero or multiple labeled hosts
  produce a red detection error and a separate manual cluster picker whose
  action is Select. Selection is locked while connected. Observability remains
  unavailable until required checks pass. Successful local connections persist
  only validated target metadata in an owner-only server-side file bound to the
  launcher-verified gcloud account. Reopen always revalidates before restoring
  access; an open browser session revalidates every ten minutes. Navigation is
  constructed first, then a visible spinner waits directly for required
  provider data without a completion-polling interval. Disconnect deletes the
  persisted target.
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
