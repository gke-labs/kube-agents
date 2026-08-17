# Kube Agents Console

The Kube Agents Console is the first-party Streamlit interface for interacting
with kube-agents and auditing their activity.

The local console reads live, bounded Cloud Logging audit records and complete
Cloud Trace records for the selected project, cluster, and time window. The
Connection onboarding page selects the Google Cloud scope and performs
bounded, read-only diagnostics before the Observability pages.

## Run locally

From the repository root:

```bash
./scripts/admin_portal.sh
```

The launcher verifies the active gcloud login, prepares `.venv` when needed,
and prints the local portal link. If authentication is missing or expired, run:

```bash
gcloud auth login
```

FastAPI owns the development portal's public listener and starts Streamlit on a
second private loopback port. The browser UI, versioned API, and Streamlit
WebSocket therefore share one origin. Both listeners use `127.0.0.1` and are not
available to other machines. To choose another public local port:

```bash
ADMIN_PORTAL_PORT=8601 ./scripts/admin_portal.sh
```

`ADMIN_PORTAL_STREAMLIT_PORT` may override the private port if its default,
`ADMIN_PORTAL_PORT + 1`, is occupied.

This loopback-only launcher is the authentication boundary for the prototype.
A remotely deployed console still requires application-level authentication
and authorization as described in the design.

Interaction state and ordered API events are stored in the owner-only
`$XDG_STATE_HOME/kube-agents/admin-portal-interactions.db` (or
`~/.local/state/kube-agents/admin-portal-interactions.db`). This state can
contain portal prompts and agent responses. Terminal records are bounded to the
newest 1,000 and seven days; SQLite secure deletion is enabled. On restart, the
API marks any incomplete record failed with explicit diagnostics rather than
inferring that asynchronous work succeeded.

## Connect to kube-agents

Connection controls live at the top of Setup's **Connection** page as two
consistent levels. First, the **Project** card verifies the gcloud identity,
project access, and GKE discovery. Then the **Cluster** card verifies the
selected kube-agents runtime. Both steps use the same **Connect**,
**Connecting**, and **Connected** states. The active connection level exposes
the only **Disconnect** action; while an explicit Connect attempt is pending,
that level instead exposes the only **Abort** action. Abort immediately detaches
the pending attempt and ignores any late result. Periodic revalidation keeps the
cluster Connected and leaves Disconnect available.

One session-scoped connection controller owns both levels, their selection,
pending check, report, failure, and verified target. Connection renders that
controller; Chat and every Observability page read the same verified target
through the shared gate. URL parameters and persisted metadata are projections
of the controller and cannot independently make a page connected or
disconnected. Controller bootstrap is shared and idempotent, so a selected page
that Streamlit resumes directly after a development-server rebuild reconstructs
the same session state instead of depending on the application entry point.

The project selector suggests the provisioned target, active gcloud
configuration, saved connection, and URL selection with their source labels; it
also accepts a manually entered project ID. Project connection loads the cluster
picker but never connects a cluster automatically. A single
`kube-agents-host=true` label preselects that cluster. Zero or multiple labels
leave the user in control of the same picker with one concise caption. A cluster
failure produces one actionable error naming the failed check, observed reason,
and next action instead of a partial checklist. The full verification checklist
appears only after both levels succeed. Disconnect unwinds one level at a time:
Cluster Disconnect retains the connected project, then Project Disconnect
becomes available.

All Kubernetes-backed portal features use one shared GKE access component. It
automatically obtains the selected cluster credentials in a process-private
temporary kubeconfig, then supplies the same target and failure behavior to
connection verification, observability, scheduled cron, and Chat. It does not
modify the user's normal kubeconfig, and the temporary directory is removed
when the portal process exits.

A successful connection also persists its non-secret target metadata in
`~/.kube-agent/state/admin-portal-connection.json`. The owner-only file contains
the gcloud account, project, cluster, location, namespace, selection source, and
last verification time. It never contains an access token, refresh token, API
key, kubeconfig, prompt, transcript, or telemetry record. On browser reload or
reopen under the same launcher-verified account, that explicit lease resumes and
immediately starts live revalidation. A failed revalidation locks runtime access
and marks the retained target as requiring revalidation, so a later successful
probe can recover without another manual selection. A target supplied only by
URL, provisioning state, or project configuration remains disconnected until
the user selects Connect. Disconnecting either persisted cluster or its project
deletes the file.
Tabs backed by a persisted lease reconcile that shared file; a Disconnect or
target change in one tab promptly locks stale tabs and cannot be undone by a
late background result.
The complete data inventory, filesystem checks, trust boundary, revalidation
contract, and local verification commands live in
[`CONNECTION_SECURITY.md`](CONNECTION_SECURITY.md).

While a browser tab remains open, the connection is revalidated every ten
minutes. Revalidation runs outside Streamlit's render thread and shows its status
in the sidebar. A lightweight UI fragment observes the job, but only the
completed job result changes connection state; elapsed time is never treated as
success. This timer is still a UI-session refresh, not an unattended daemon. A
failed refresh immediately locks the provider-backed pages and requires
reconnecting. Credential refresh remains owned by the Google Cloud CLI
credential store; the portal mints short-lived tokens only for individual checks
and discards them.

Project **Connect**, followed by Cluster **Connect**, performs bounded,
read-only checks for:

- gcloud CLI and Application Default Credentials
- selected-project access and required APIs
- GKE cluster and `kube-agents-host=true` discovery
- recent Cloud Logging and structured agent audit records
- recent Cloud Trace data
- persisted agent chat history through a fixed, read-only in-pod query

After a successful connection, the **Connection** page displays the resulting
checklist and distinguishes permission, authentication, API, connectivity, and
no-recent-data outcomes. Cluster connectivity depends on the identity, project,
GKE, and agent-runtime checks; an unavailable Logging or Trace source remains a
visible checklist failure but does not lock runtime-backed Chat, Task Kanban, or
Scheduled Cron. A failed cluster connection shows one actionable error by the
controls. The portal never grants IAM, enables APIs, changes Kubernetes
resources, or retains access tokens.
Observe and Chat pages are unavailable until the required checks pass for the
selected project and cluster.

GKE and Logging probes use the launcher-verified gcloud account explicitly.
Cloud Trace uses Application Default Credentials because some enterprise CLI
credential types cannot call the Trace REST endpoint directly. If that check
fails:

```bash
gcloud auth application-default login
```

## Chat

Chat is always available in navigation. Without a verified target it provides a
message directing the user to Connection. A full-width,
filterable session table shows last activity, source, user, subject,
message count, and tool count. The table has 25-row, URL-persisted pagination.
Selecting a row updates the URL-selected session and renders its transcript and
composer below the table.

Portal-owned sessions use the connected deployment's default Hermes profile,
the same front-door Chat Agent used by Google Chat and Slack. The Chat page uses
the versioned `/api/v1` contract for agent discovery, session reads, messages,
linked tasks, new interactions, and approvals; it does not call the Hermes
adapter directly. The selected session is stored in the URL and its transcript
is reloaded from Hermes after a refresh. Google Chat and Slack sessions are
visible but read-only; a portal follow-up creates a separate `portal_*` session
so the console does not impersonate an external participant or unexpectedly
post into a third-party thread.

Specialist work created from a session is joined through the Task Kanban
task's trusted `session_id`. The thread renders each linked task's assignee,
status, run count, latest lifecycle event, retry failure, completion summary,
or terminal error inline. Threads from every chat surface poll this bounded
read model every five seconds only while linked work is runnable. Stable task
cards remain visible while a small status indicator refreshes; polling stops
when all work is terminal. Task IDs link to the exact Task Kanban record.

This polling projection is intentionally different from Google Chat and Slack
delivery. Those adapters own a durable chat and thread destination that the
Hermes Kanban notifier can post to. The local API run is represented upstream
as an ephemeral TUI `run_*` destination with no connected TUI notification
consumer, so notifier messages cannot be the portal's source of truth. Task
results remain joined to the portal transcript visually; they are not inserted
as synthetic agent chat messages.

The FastAPI interaction resource joins the root Hermes run with delegated Task
Kanban work. A root run ending does not make the interaction terminal: the API
waits for linked work to settle and returns explicit failure diagnostics when a
task fails or its state cannot be read. This is the same black-box contract used
by the Streamlit page and evaluation clients. The complete contract and
deployment boundary are owned by the
[admin-console design](../docs/designs/admin-console.md#portal-api-and-shared-chat-abstraction).

The portal does not retrieve the external Hermes API key. The transitional
adapter runs a fixed, size-bounded client inside the selected `platform-agent`
container. That in-container process reads `API_SERVER_KEY` from its own
environment and uses it only for the loopback request; the credential never
enters the local portal process, stdout, or kubectl arguments. User prompts are
sent over stdin rather than command arguments. If Hermes requests a tool
approval, the API and UI permit only **Approve once** or **Deny**; permanent and
bulk approvals are deliberately unavailable.

Before starting a `portal_*` run, the fixed client records the launcher-verified
gcloud account as session metadata with source `admin_portal`. The write is
parameterized and cannot modify external session IDs. Production should move
this attribution into the dedicated chat API rather than writing the shared
metadata store from the local client.

The History view retains the existing read-only, cross-user projection of
persisted Hermes sessions.

## Observability

The Observability navigation group contains Overview, Activity Explorer, Task
Kanban, and Scheduled Cron. The pages remain visible before connection and
share one connection-gate component that directs the user to Connection;
provider-backed content becomes available after connecting to a verified
kube-agents host.

Task Kanban reads the selected Agent's live shared board. Today each Agent entry is
backed by a Kubernetes `PlatformAgent` custom resource. The page summarizes
open, attention, and completed work, filters by status and assignee, and uses a
25-row selectable table with URL-persisted pagination. Selecting a row keeps
the task in the URL and renders its details below the table. Task inspection
includes the request, assignee, priority, current state, timestamps, linked chat
session, parent and child tasks, delivery subscription health, attachments,
the newest 100 retained runs with the total count and truncation state,
completion metadata, comments, and lifecycle events.

The board and task queries are fixed, read-only, and bounded. Credential-shaped
values are redacted before rendering, raw delivery destinations and attachment
storage paths are not returned, and the page never claims, retries, comments
on, or otherwise changes a task.

Scheduled Cron reads every bounded Hermes profile cron store in the selected
Agent. Its execution table aggregates the selected history into one row per job
title with run and outcome counts, latest activity, and participating profiles.
Each row expands in place to list every retained start time, trigger, duration,
status, profile, and error—there is no separate detail table. The page also shows
configured cadence and task, last and next runs, and a UTC calendar of recent
activity plus every projected occurrence in the next 21 days. High-frequency
calendar occurrences are summarized by job and day. Scheduler health comes
from each profile's ticker heartbeat. An enabled definition without a live
ticker is reported as unable to run automatically instead of being presented
as healthy.

## Live activity

Use Connection to select the URL-persisted project and cluster. Overview and
Activity Explorer keep their activity scope controls on the page. Chat History
and Scheduled Cron have separate retained-history windows. The activity pages
read:

- structured application audit events from Cloud Logging, including Fluent Bit
  wrapped JSON records; and
- complete Hermes traces that contain the trusted `session.id` label.

Chat History reads persisted user and assistant messages from every Hermes
profile in the selected Agent runtime. Reads are bounded and credential-shaped
values are redacted. Sessions without trusted user metadata remain explicitly
unattributed; tool output and model reasoning are not rendered. URL state keeps
the selected agent, profile, platform, opaque user filter, and session.
Free-text search stays out of the URL because it may contain sensitive data.

The Activity Explorer retains each Logging insert ID or Trace/span ID, provides
a Google Cloud evidence link, and shows scrubbed, size-bounded evidence fields.
The causal flow is intentionally narrower than the evidence ledger: it uses
Cloud Trace parent/child lineage to show individual `api.*` LLM requests,
LLM-child tool calls and approvals, and agent skill loads. A typed source
normalizer groups by raw OTel `user.id`/`hermes.sender.id`, then
`chat.platform`, cron job parsed from `session.id`, explicit trigger, or
`hermes.session.kind`, falling back to the unmodified `session.id`. The node
retains every available origin field and scope, the distinct-session count, and
bounded raw-value samples; it
never converts `k8s-watcher` into a generic Human or Event label. Aggregate
`llm.*`/turn wrappers, gateway/chat lifecycle spans, and parallel Cloud Logging
delivery records remain available in Timeline and the ledger but do not inflate
the flow. Fluent Bit is treated as a log collector, not an agent; when an audit
payload has no profile identity, the ledger labels the source `gateway-runtime`
and retains `fluent-bit` as collector evidence.
Source errors and result-limit truncation are displayed rather than silently
treated as a complete result. Both activity pages show one loading
indicator while the initial snapshot, refresh, or next source pages are read.
Logging and Trace start with two bounded pages and retain their opaque
continuation tokens only in the UI session; **Load more activity** appends the
next pages without rereading earlier results. Logging uses 500-record pages, a
60-second per-page timeout, and a shared 90-second load deadline. A later-page
failure retains the earlier pages and is reported as partial data. Trace and
each of Logging's two non-overlapping queries stop after ten pages; the UI asks
the user to narrow the time window when a query reaches that ceiling. The
forensic ledger separately paginates the loaded events at 50 rows per page so
source completeness does not create an oversized browser table.

The provider is a read model, not a perfect causal join. Older records without
trusted interaction, user, task, or proxy request identifiers remain labeled
as missing. The ledger page and selected event are persisted in the URL;
free-text and facet filters remain local to the browser session.

## Validate

```bash
python3 -m unittest discover -s admin_console/tests -v
python3 -m compileall -q admin_console
```

The product and integration design is owned by
[`docs/designs/admin-console.md`](../docs/designs/admin-console.md).
