---
title: ChatOps
description: Google Chat and Slack are the primary interfaces to the harness. Both terminate at the Chat Agent front door, which delegates to the Platform Agent.
sidebar:
  order: 3
---

Chat is the harness's primary interface — for both requests from humans and proactive alerts from cron watchdogs. The channels shipping today are **Google Chat** (the reference channel, fully wired and E2E tested; enable with `GOOGLE_CHAT_ENABLED=true` during provisioning) and **Slack** (enable with `SLACK_ENABLED=true` during provisioning). Both are opt-in and default to disabled.

Both channels terminate at the **Chat Agent** — the `default` Hermes profile in the agent pod, and the only profile that receives chat ingress. It discovers which specialists exist (via its `router` MCP tool `list_agents`), delegates the request to the right one as a card on the shared **kanban board** (`kanban_create`), and relays progress and results back into the thread. The [Platform Agent](/kube-agents/concepts/platform-agent/) does the actual infrastructure work as a delegated kanban worker, and per-cluster [Cluster Agents](/kube-agents/concepts/cluster-agents/) handle single-cluster runtime debugging; neither receives chat directly. A user still sees a single conversational agent regardless of channel — the delegation is visible only as progress updates in the thread. The design of record for this coordination model is [`docs/designs/agent-communication.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/agent-communication.md).

## Google Chat

Google Chat is the reference channel. Setup is automated by the provisioner (`provision_05_gcp_gchat.sh`), which runs when `GOOGLE_CHAT_ENABLED=true`.

### How it's wired

- A **Pub/Sub topic** and **subscription** are created in the target GCP project.
- Your Google Chat app (configured separately in the [Chat API console](https://console.cloud.google.com/apis/api/chat.googleapis.com)) publishes events to the topic.
- The Chat Agent (the pod's `default` Hermes profile) consumes the subscription through Hermes' bundled Google Chat adapter, configured by the `platforms.google_chat` block of [`agents/chat/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/config.yaml).
- Environment variables `GOOGLE_CHAT_PROJECT_ID` and `GOOGLE_CHAT_SUBSCRIPTION_NAME` are wired into the pod by the operator.

### Allowed users

Google Chat ingress can be gated by `GOOGLE_CHAT_ALLOWED_USERS` (a comma-separated list of user emails, collected by the provisioner as `ALLOWED_USERS`). Leaving it empty allows all users — the operator sets `GOOGLE_CHAT_ALLOW_ALL_USERS=true` in that case.

### What it looks like end to end

1. User DMs the app or @-mentions it in a space.
2. Chat sends the message event to the topic; the Chat Agent consumes it from the subscription.
3. The Chat Agent picks the right specialist (via `list_agents`) and files a kanban card with the full request context (`kanban_create`).
4. The gateway's kanban dispatcher spawns the specialist — for infrastructure work, the Platform Agent (`hermes -p platform`) — which runs the tool loop and completes the card with a summary.
5. The completion posts back into the same thread (the originating chat session is auto-subscribed to the card), with the Chat Agent relaying progress along the way.

### E2E coverage

The Google Chat path has an end-to-end integration test suite in [`tests/e2e/`](https://github.com/gke-labs/kube-agents/tree/main/tests/e2e). It runs a real Chat message through the deployed agent and asserts a valid reply, giving CI a signal on the full stack.

### Session metadata

Every Chat message carries session context (space, user, thread) that flows through Hermes and out as OpenTelemetry spans. The full trace is documented in [`docs/designs/gchat-session-metadata-data-flow.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/gchat-session-metadata-data-flow.md).

## Slack

Slack is opt-in. Configure with `SLACK_ENABLED=true` during provisioning; the provisioner will prompt for the token values below.

### How it's wired

- `provision_06_slack.sh` collects `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_ALLOWED_USERS`, `SLACK_HOME_CHANNEL`, and `SLACK_HOME_CHANNEL_NAME`, and stores them as Kubernetes secrets.
- The Slack listener itself lives inside the Hermes runtime; it uses Socket Mode (no public webhook required) driven by the app token.
- Setup for the Slack app itself (creating the app, generating tokens, installing to workspace) is documented in the Hermes docs: [hermes-agent.nousresearch.com/docs/user-guide/messaging/slack](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/slack).

### Allowed users

Slack ingress is gated by `SLACK_ALLOWED_USERS` (a comma-separated list of Slack user IDs). Messages from users not on the list are silently ignored — a per-channel allowlist for the harness. Leaving it empty allows all users (the operator sets `SLACK_ALLOW_ALL_USERS=true` in that case).

### Home channel

`SLACK_HOME_CHANNEL` designates the channel proactive watchdog alerts land in when no user thread is involved. Set it to a monitoring/oncall channel your team already watches.

## Proactive alerts (both channels)

The harness doesn't only reply to messages. When a cron watchdog finds something worth surfacing (a security patch is available, a PR was opened, a cluster is drifting from blueprint), the alert posts to the configured Chat channel unprompted:

- **Google Chat**: to the space that owns the interaction, or the space set via `GOOGLE_CHAT_HOME_CHANNEL`.
- **Slack**: to `SLACK_HOME_CHANNEL`.

See [Proactive autonomy](/kube-agents/overview/proactive-autonomy/) for what triggers these alerts and [Autonomous watchdogs](/kube-agents/concepts/autonomous-watchdogs/) for the schedules.

## First-run onboarding

On a fresh install the first chat interaction gets a guided onboarding instead of a cold start. Two `no_agent` cron jobs on the Chat Agent profile (`agents/chat/defaults/cron/jobs.json`) drive it:

- **`bootstrap-inventory-scan`** files a kanban card assigned to the Platform Agent (with a fixed idempotency key, so re-firing never stacks duplicates). That worker runs the environment-discovery SOP ([`agents/platform/governance/inventory.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/governance/inventory.md)) — fleet topology, Workload Identity, workload SRE posture — and writes a presentation-ready report to `/opt/data/INVENTORY.md`.
- **`bootstrap-inventory-delivery`** posts that report **verbatim** into the chat once two conditions hold: the scan has finished, and a human has connected.

The `bootstrap_onboarding` plugin (enabled in `agents/chat/config.yaml`) hooks the first human turn: it greets the user, binds the delivery job to that chat thread, and marks that a human is present. Once the report is delivered, the flow marks itself complete and removes its own jobs — it never runs again on that data volume. The full design, state markers, and maintenance rules live in the plugin's [README](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/defaults/plugins/bootstrap_onboarding/README.md).

## What's not here

- **No web UI.** Chat is the primary surface.
- **No CLI beyond port-forwarding to the Hermes API.** For debug you can `kubectl port-forward` to the agent pod and use the Hermes CLI directly — note the pod hosts several profiles, so a bare `hermes` command talks to the locked-down Chat Agent; use `hermes -p platform` to reach the Platform Agent (or `hermes -p <cluster-profile>` for a Cluster Agent). This isn't a user-facing pattern.
- **No email, PagerDuty, or generic webhook ingress.** Chat channels only.

## Where to go next

- [Overview → Proactive autonomy](/kube-agents/overview/proactive-autonomy/) — what fires the outbound alerts.
- [Concepts → Observability](/kube-agents/concepts/observability/) — where the traces from chat sessions land.
