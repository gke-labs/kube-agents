---
title: Manual install
description: Install the Platform Agent workspace into an existing Hermes-compatible harness by hand.
---

If you're not using GKE or the shipping `./provision.sh` flow, you can install the Platform Agent workspace into an existing Hermes-compatible harness by hand. This page covers the harness-side setup only; you still need to arrange cluster access, chat ingress, an inference gateway, and (for GitOps flows) a token minter separately.

## Prerequisites

- A harness or platform that runs the Hermes agent runtime (`nousresearch/hermes-agent`) with workspace file access and tool execution.
- `kubectl` configured with access to the target Kubernetes cluster(s).
- [cert-manager](/kube-agents/install/prerequisites/#cert-manager-on-the-target-cluster) v1.13.0+ on any cluster where you plan to install the operator's webhook.

## Step 1: Copy the workspace

The Platform Agent needs a dedicated workspace directory containing its persona, config, skills, governance SOPs, and cron definitions.

```bash
cp -r agents/platform /path/to/harness/workspace/agents/platform
```

The directory layout your harness will see:

```
platform/
├── SOUL.md                  # persona / system prompt
├── AGENTS.md                # workspace runtime instructions
├── config.yaml              # MCP servers, toolsets, plugins
├── skills/                  # SKILL.md bundles
├── governance/              # SOPs invoked by cron jobs
├── cron/jobs.json           # scheduled autonomous jobs
├── plugins/                 # in-tree Hermes plugins
├── defaults/                # hooks + plugin defaults
└── scripts/                 # in-pod Python MCP server
```

## Step 2: Register the agent

Configure your harness to register a new agent named `platform`:

- **Workspace directory**: the `platform/` directory copied in step 1.
- **System prompt**: load from `SOUL.md`.
- **Config**: load MCP servers, toolsets, and plugins from `config.yaml`.
- **Skills**: point the harness at `skills/` (the runtime discovers `SKILL.md` files automatically).
- **Registration**: perform the platform-specific agent registration and reload/restart the harness if required.

## Step 3: Configure the heartbeat

The Platform Agent expects a scheduled heartbeat for routine maintenance and drift detection. Configure a recurring task:

- **Schedule**: every 1 minute (`* * * * *`)
- **Target agent**: `platform`
- **Message**:

  ```text
  [Scheduled Heartbeat]
  Read HEARTBEAT.md and execute due checks.
  Update memory/heartbeat-state.json with fresh timestamps/results.
  If healthy and no anomalies, respond exactly NO_REPLY; otherwise return concise blockers.
  ```

If your harness has native cron support (Hermes does, via `cron/jobs.json`), the governance watchdogs will register automatically once the workspace is loaded. Otherwise wire them by hand from `cron/jobs.json`.

## Step 4: Wire the surrounding infrastructure

The manual install covers only the agent workspace. To reach parity with a `./provision.sh` install, you still need:

- **Cluster access**: a Kubernetes context the agent can call. The shipping config expects the [GKE MCP server](https://container.googleapis.com/mcp) proxied via `mcp-remote`; for other clusters, substitute your own Kubernetes MCP server or add `kubectl` to the toolset.
- **Chat ingress**: Google Chat Pub/Sub or Slack Socket Mode. See [ChatOps](/kube-agents/concepts/chatops/).
- **Inference gateway**: any Completions-API-compatible endpoint (LiteLLM is the default recommendation). See [Inference gateway](/kube-agents/concepts/inference-gateway/).
- **GitHub token minter**: for the `submit-suggestion` and `github-issue-resolver` skills. See [Deploy → Token minter](/kube-agents/deploy/token-minter/).

## Verify

Interact with the agent through your harness's chat surface. It should respond with a status update and, on the next heartbeat tick, begin running the governance SOPs on schedule.

## Post-install

- Read [SOUL.md](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/SOUL.md) to understand the persona's guardrails.
- Review the [skill catalog](/kube-agents/skills/) to see what the agent can do on request.
- Review [Autonomous watchdogs](/kube-agents/concepts/autonomous-watchdogs/) to understand what runs on its own.
