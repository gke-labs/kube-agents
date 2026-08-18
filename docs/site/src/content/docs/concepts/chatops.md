---
title: ChatOps
description: Google Chat and Slack are the primary interfaces to the harness. Both terminate at the Planning Agent front door, which delegates to the Platform Agent.
sidebar:
  order: 3
---

Chat is the harness's primary interface — for requests from humans and for the unprompted messages the harness raises itself ([Proactive alerts](#proactive-alerts-both-channels)). The channels shipping today are **Google Chat** (the reference channel, fully wired and E2E tested; enable with `GOOGLE_CHAT_ENABLED=true` during provisioning) and **Slack** (enable with `SLACK_ENABLED=true` during provisioning). Both are opt-in and default to disabled.

Both channels terminate at the **Planning Agent** — the `default` Hermes profile in the agent pod, and the only profile that receives chat ingress. It knows which specialists exist because the roster is injected into every turn by the `agent_roster` plugin (its `router` MCP tool `list_agents` re-reads the same list on demand), and files the work as a card on the shared **kanban board** (`kanban_create`), assigned to the specialist that can execute it. Results come back on their own: the gateway posts each completed card's answer into the thread verbatim, and the Planning Agent handles the hand-off and anything that blocks or fails. The [Platform Agent](/kube-agents/concepts/platform-agent/) does the actual infrastructure work as a delegated kanban worker, and per-cluster [Cluster Agents](/kube-agents/concepts/cluster-agents/) handle single-cluster runtime debugging; neither receives chat directly. A user still sees a single conversational agent regardless of channel — the delegation is visible only as progress updates in the thread. The design of record for this coordination model is [`docs/designs/agent-communication.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/designs/agent-communication.md).

## Google Chat

Google Chat is the reference channel. Setup is automated by the provisioner (`provision_05_gcp_gchat.sh`), which runs when `GOOGLE_CHAT_ENABLED=true`.

### How it's wired

- A **Pub/Sub topic** and **subscription** are created in the target GCP project.
- Your Google Chat app (configured separately in the [Chat API console](https://console.cloud.google.com/apis/api/chat.googleapis.com)) publishes events to the topic.
- The Planning Agent (the pod's `default` Hermes profile) consumes the subscription through Hermes' bundled Google Chat adapter, configured by the `platforms.google_chat` block of [`agents/chat/config.yaml`](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/config.yaml).
- Environment variables `GOOGLE_CHAT_PROJECT_ID` and `GOOGLE_CHAT_SUBSCRIPTION_NAME` are wired into the pod by the operator.

### Allowed users

Google Chat ingress can be gated by `GOOGLE_CHAT_ALLOWED_USERS` (a comma-separated list of user emails, collected by the provisioner as `ALLOWED_USERS`). Leaving it empty allows all users — the operator sets `GOOGLE_CHAT_ALLOW_ALL_USERS=true` in that case.

### Home channel

`spec.integration.googleChat.homeChannel` on the PlatformAgent (surfaced to the pod as `GOOGLE_CHAT_HOME_CHANNEL`) is the space a notification lands in when there is no thread to reply into — which is every alert-driven investigation, since nobody started the conversation.

Unlike Slack's, this one is not covered by `/sethome`. That command writes the **Planning Agent** profile, which is enough for the gateway's own delivery, but a specialist runs as a kanban worker against its own profile and reads the value from the pod environment instead. Set the field on the resource. `provision_05_gcp_gchat.sh` does not collect it today, so on a stock install it is empty and alert-driven reports have nowhere to go — the investigation still runs and still opens its remediation PR, so the only visible symptom is silence in chat.

### What it looks like end to end

1. User DMs the app or @-mentions it in a space.
2. Chat sends the message event to the topic; the Planning Agent consumes it from the subscription.
3. The Planning Agent picks the right specialist from the injected roster and files a kanban card with the full request context (`kanban_create`).
4. The gateway's kanban dispatcher spawns the specialist — for infrastructure work, the Platform Agent (`hermes -p platform`) — which runs the tool loop and completes the card with a one-line `summary` and the full answer in `result`.
5. The originating chat session is auto-subscribed to the card, so the thread fills itself: each `kanban_heartbeat(note=…)` milestone adds a `⏳` line to one rolling message that the notifier edits in place while the specialist works — so a card with five milestones notifies the space once, not five times — and the completion posts as a separate `✔ … done` message carrying the `result` verbatim. The rolling message is then marked finished (`✓`, or `⏹` for a card that failed). The notifier delivers all of it directly and the Planning Agent is woken for none of it; it is woken when a card blocks or fails.

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

### Slash commands

Slack only routes a leading-slash message to the app's slash handler if that slash is **registered on the Slack app**, and provisioning creates the app from tokens alone. Registering them is an optional post-provisioning step — see [INSTALL.md](https://github.com/gke-labs/kube-agents/blob/main/INSTALL.md#2-slack-configuration-slack_enabledtrue) for the procedure.

Until you do, a typed `/hermes <subcommand>` arrives as an ordinary channel message rather than a command. The `legacy_slash_commands` plugin on the Planning Agent profile unwraps that form before the gateway resolves it, so `/hermes sethome` behaves as `/sethome` either way — registering the slashes adds Slack's autocomplete, not the behaviour. The plugin's [README](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/defaults/plugins/legacy_slash_commands/README.md) is the design of record.

### Home channel

`SLACK_HOME_CHANNEL` designates the channel an unprompted message lands in when no user thread is involved. Set it to a monitoring/oncall channel your team already watches.

It is optional at provisioning time: leave the prompt empty and set it later from Slack by running `/sethome` (or `/hermes sethome`) in the channel you want. That writes the value into the **Planning Agent** profile — the one that owns Slack ingress — which is why the command has to run through the gateway rather than being applied by an agent on its own profile.

A scheduled brief posts flat in that channel, never inside a thread. `/sethome` also records whichever thread it happened to be typed in, and threading every scheduled report under one ageing thread leaves only the first one visible — so cron delivery drops the thread deliberately. A job that wants its output in a thread names an explicit `deliver=` target instead.

## Proactive alerts (both channels)

The harness doesn't only reply to messages. A cluster event posted to the in-pod triage endpoint (`inject_message` in [`agents/platform/scripts/session_kv_server.py`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/scripts/session_kv_server.py)) opens a thread unprompted: it posts the alert first, then runs the triage turn in the thread that alert created. The first-run inventory report arrives the same way, into the thread `bootstrap_onboarding` bound. Where an unprompted message lands:

- **Google Chat**: to the space that owns the interaction, or the space set via `GOOGLE_CHAT_HOME_CHANNEL`.
- **Slack**: to `SLACK_HOME_CHANNEL`.

A governance watchdog's findings are not on this path. An audit publishes to its ledger issue and to the remediation pull requests that link back to it, so the report to read is the issue rather than a channel message — see [Proactive autonomy](/kube-agents/overview/proactive-autonomy/) and [Autonomous watchdogs](/kube-agents/concepts/autonomous-watchdogs/) for the schedules.

## First-run onboarding

On a fresh install the first chat interaction gets a guided onboarding instead of a cold start. Two `no_agent` cron jobs on the Planning Agent profile (`agents/chat/defaults/cron/jobs.json`) drive it:

- **`bootstrap-inventory-scan`** files a kanban card assigned to the Platform Agent, recording the card's id in `/opt/data/.bootstrap_scan_filed` so it never files a second one. That worker runs the environment-discovery SOP ([`agents/platform/governance/inventory.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/governance/inventory.md)) — fleet topology, Workload Identity, workload SRE posture — and writes the complete findings to `/opt/data/INVENTORY.raw.md`. It then files a second card, which runs the prioritization SOP ([`agents/platform/governance/inventory_prioritize_sop.md`](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/governance/inventory_prioritize_sop.md)) against those findings alone and writes the ranked report the user is sent to `/opt/data/INVENTORY.md`. The full findings stay on disk, and the report says how many items it left out.
- **`bootstrap-inventory-delivery`** posts that report **verbatim** into the chat once two conditions hold: the ranked report has been written, and a human has connected. It claims the delivery atomically first, so overlapping runs can't post the report twice.

The `bootstrap_onboarding` plugin (enabled in `agents/chat/config.yaml`) hooks the first human turn: it greets the user, binds the delivery job to that chat thread, and marks that a human is present. Once the report is delivered, the flow marks itself complete and removes its own jobs — it never runs again on that data volume.

Both jobs fire every minute (`* * * * *`, see [Autonomous watchdogs](/kube-agents/concepts/autonomous-watchdogs/#what-fires-the-schedule)) while the work they guard takes minutes, so each stage records its own marker the moment it acts rather than inferring from the report or from completion. The full design, state markers, and maintenance rules live in the plugin's [README](https://github.com/gke-labs/kube-agents/blob/main/agents/chat/defaults/plugins/bootstrap_onboarding/README.md).

## What's not here

- **No web UI.** Chat is the primary surface.
- **No CLI beyond port-forwarding to the Hermes API.** For debug you can `kubectl port-forward` to the agent pod and use the Hermes CLI directly — note the pod hosts several profiles, so a bare `hermes` command talks to the locked-down Planning Agent; use `hermes -p platform` to reach the Platform Agent (or `hermes -p <cluster-profile>` for a Cluster Agent). This isn't a user-facing pattern.
- **No email, PagerDuty, or generic webhook ingress.** Chat channels only.

## Where to go next

- [Overview → Proactive autonomy](/kube-agents/overview/proactive-autonomy/) — what fires the outbound alerts.
- [Concepts → Observability](/kube-agents/concepts/observability/) — where the traces from chat sessions land.
