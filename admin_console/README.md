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

Connection controls live at the top of Setup's **Connection** page. One editable
project selector suggests the provisioned target, active gcloud configuration,
saved connection, and URL selection with their source labels; it also accepts a
manually entered project ID. **Connect** discovers
the single GKE cluster labeled `kube-agents-host=true` and selects it
automatically. If no labeled host or multiple labeled hosts are found, the
portal shows a red detection error and a separate cluster picker with a
**Select** action. Project and cluster selection are locked while connected.
Connect and Disconnect are mutually exclusive, and Disconnect retains the
selected URL scope for a quick reconnect.

A successful connection also persists its non-secret target metadata in
`$XDG_STATE_HOME/kube-agents/admin-portal-connection.json` (or
`~/.local/state/kube-agents/admin-portal-connection.json`). The owner-only file
contains the gcloud account, project, cluster, location, namespace, selection
source, and last verification time. It never contains an access token, refresh
token, API key, kubeconfig, prompt, transcript, or telemetry record. On browser
reload or reopen, the portal revalidates this target before enabling Chat or
Observability. **Disconnect** deletes the file.

While a browser tab remains open, the connection is revalidated every ten
minutes. Navigation renders first; when restore or revalidation data is needed,
the portal runs the read-only network checks outside Streamlit's render thread
and shows their status in the sidebar. A lightweight UI fragment observes the
job, but only the completed job result changes connection state; elapsed time is
never treated as success. This timer is still a UI-session refresh, not an
unattended daemon. A failed refresh immediately locks the provider-backed pages
and requires reconnecting. Credential refresh remains owned by the Google Cloud
CLI credential store; the portal mints short-lived tokens only for individual
checks and discards them.

Select **Connect** to perform bounded, read-only checks for:

- gcloud CLI and Application Default Credentials
- selected-project access and required APIs
- GKE cluster and `kube-agents-host=true` discovery
- recent Cloud Logging and structured agent audit records
- recent Cloud Trace data
- persisted agent chat history through a fixed, read-only in-pod query

The **Connection** page displays the resulting checklist, distinguishes
permission, authentication, API, connectivity, and no-recent-data outcomes, and
provides remediation guidance directly below the connection controls. The portal
never grants IAM, enables APIs, changes Kubernetes resources, or retains access
tokens.
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
Agent. It shows active and recent executions, configured cadence and task,
manual versus scheduled triggers, last and next runs, and a UTC calendar of
recent activity plus every projected occurrence in the next 21 days.
High-frequency occurrences are summarized by job and day. Scheduler health
comes from each profile's ticker heartbeat. An enabled definition without a
live ticker is reported as unable to run automatically instead of being
presented as healthy.

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
Records with missing or inferred attribution are excluded from the causal
Sankey. Source errors and result-limit truncation are displayed rather than
silently treated as a complete result.

The provider is a read model, not a perfect causal join. Older records without
trusted interaction, user, task, or proxy request identifiers remain labeled
as missing. Page-local search and ledger selections are not yet persisted in
the URL.

## Validate

```bash
python3 -m unittest discover -s admin_console/tests -v
python3 -m compileall -q admin_console
```

The product and integration design is owned by
[`docs/designs/admin-console.md`](../docs/designs/admin-console.md).
