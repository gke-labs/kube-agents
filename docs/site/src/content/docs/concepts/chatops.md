---
title: ChatOps
description: Google Chat and Slack are the primary interfaces to the Platform Agent. Both terminate at the Platform Agent Deployment.
sidebar:
  order: 2
---

Chat is the Platform Agent's primary interface — for both requests from humans and proactive alerts from cron watchdogs. The channels shipping today are **Google Chat** (default, fully wired, E2E tested) and **Slack** (opt-in via `SLACK_ENABLED=true` during provisioning). Both terminate at the same Platform Agent Deployment, so a user only sees one agent regardless of channel.

## Google Chat

Google Chat is the default channel. Setup is automated by the provisioner (`provision_05_gcp_gchat.sh`).

### How it's wired

- A **Pub/Sub topic** and **subscription** are created in the target GCP project.
- Your Google Chat app (configured separately in the [Chat API console](https://console.cloud.google.com/apis/api/chat.googleapis.com)) publishes events to the topic.
- The Platform Agent pod runs the `platform_control` MCP server, which consumes the subscription.
- Environment variables `GOOGLE_CHAT_PROJECT_ID` and `GOOGLE_CHAT_SUBSCRIPTION_NAME` are wired into `config.yaml`.

### What it looks like end to end

1. User DMs the app or @-mentions it in a space.
2. Chat sends the message event to the topic.
3. `platform_control` receives it, hands off to Hermes.
4. Hermes runs the tool loop, produces a reply.
5. The reply posts back to the same thread.

### E2E coverage

The Google Chat path has an end-to-end integration test suite in [`tests/e2e/`](https://github.com/gke-labs/kube-agents/tree/main/tests/e2e) (introduced in [PR #324](https://github.com/gke-labs/kube-agents/pull/324)). It runs a real Chat message through the deployed agent and asserts a valid reply, giving CI a signal on the full stack.

### Session metadata

Every Chat message carries session context (space, user, thread) that flows through Hermes and out as OpenTelemetry spans. The full trace is documented in [`docs/gchat-session-metadata-data-flow.md`](https://github.com/gke-labs/kube-agents/blob/main/docs/gchat-session-metadata-data-flow.md).

## Slack

Slack is opt-in. Configure with `SLACK_ENABLED=true` during provisioning; the provisioner will prompt for the token values below.

### How it's wired

- `provision_06_slack.sh` collects `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_ALLOWED_USERS`, `SLACK_HOME_CHANNEL`, and `SLACK_HOME_CHANNEL_NAME`, and stores them as Kubernetes secrets.
- The Slack listener itself lives inside the Hermes runtime; it uses Socket Mode (no public webhook required) driven by the app token.
- Setup for the Slack app itself (creating the app, generating tokens, installing to workspace) is documented in the Hermes docs: [hermes-agent.nousresearch.com/docs/user-guide/messaging/slack](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/slack). [PR #352](https://github.com/gke-labs/kube-agents/pull/352) added this link to the provisioner output.

### Allowed users

Slack ingress is gated by `SLACK_ALLOWED_USERS` (a comma-separated list of Slack user IDs). Messages from users not on the list are silently ignored — a per-channel allowlist for the harness.

### Home channel

`SLACK_HOME_CHANNEL` designates the channel proactive watchdog alerts land in when no user thread is involved. Set it to a monitoring/oncall channel your team already watches.

## Proactive alerts (both channels)

The Platform Agent doesn't only reply to messages. When a cron watchdog finds something worth surfacing (a security patch is available, a PR was opened, a cluster is drifting from blueprint), it posts to the configured Chat channel unprompted:

- **Google Chat**: to the space that owns the interaction, or a configured monitoring space.
- **Slack**: to `SLACK_HOME_CHANNEL`.

See [Proactive autonomy](/kube-agents/overview/proactive-autonomy/) for what triggers these alerts and [Autonomous watchdogs](/kube-agents/concepts/autonomous-watchdogs/) for the schedules.

## What's not here

- **No web UI.** Chat is the primary surface.
- **No CLI beyond port-forwarding to the Hermes API.** For debug you can `kubectl port-forward` to the Platform Agent pod and use the Hermes CLI directly, but this isn't a user-facing pattern.
- **No email, PagerDuty, or generic webhook ingress.** Chat channels only.

## Where to go next

- [Overview → Proactive autonomy](/kube-agents/overview/proactive-autonomy/) — what fires the outbound alerts.
- [Concepts → Observability](/kube-agents/concepts/observability/) — where the traces from chat sessions land.
