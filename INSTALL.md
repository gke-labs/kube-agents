# Platform Agent Installation Guide

This guide explains how to install and configure the Platform Agent within an AI agent harness.

The Platform Agent acts as the master custodian and architect, responsible for multi-tenancy governance and dynamic provisioning of specialized subagents (`devteam` and `operator`).

## Prerequisites

- An AI agent harness capable of running autonomous agents with workspace file access and tool execution capabilities.
- Kubernetes CLI (`kubectl`) configured with access to your target GKE clusters.

## Installation Steps

### 1. Workspace Setup

The Platform Agent requires a dedicated workspace directory containing its core instructions, identity, and skills.

Copy the pre-packaged platform workspace to your agent harness's workspace directory:

```bash
# Assuming your agent harness workspace is at /path/to/harness/workspace/agents
cp -r workspace/agents/platform /path/to/harness/workspace/agents/platform
```

### 2. Agent Registration

Configure your AI agent harness to register a new agent named `platform`.

- **Workspace Directory**: Set the agent's workspace to the `platform` directory copied in Step 1.
- **System Prompt / Core Instructions**: Load the agent's primary instructions from `SOUL.md`.
- **Identity**: Load the agent's persona and constraints from `IDENTITY.md`.
- **Tools / Skills**: Ensure the agent has access to the skills defined in the `skills/` subdirectory (specifically `platform-agent-provisioner`).
- **Registration**: perform platform-specific agent registration (as required by your harness) as a first-class agent (not a transient sub-agent). Ensure all other agents can see and address it (e.g. mention and @handle it). If reload/restart is needed request user to perform the restart.

### 3. Heartbeat Schedule Configuration

The Platform Agent relies on a scheduled heartbeat to perform routine maintenance, drift detection, and fleet audits.

Configure a recurring scheduled task (cron) within your agent harness for the `platform` agent:

- **Schedule**: Every 1 minute (`1m` or `* * * * *`)
- **Target Agent**: `platform`
- **Message Content**:
  ```text
  [Scheduled Heartbeat]
  Read HEARTBEAT.md and execute due checks.
  Update memory/heartbeat-state.json with fresh timestamps/results.
  If healthy and no anomalies, respond exactly NO_REPLY; otherwise return concise blockers.
  ```

## Post-Installation

Once installed and the heartbeat is active, the Platform Agent will begin monitoring its state. You can interact with it directly to request provisioning of new application scopes or cluster operators.
