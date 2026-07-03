# Platform Agent Installation Guide

This guide explains how to install and configure the Platform Agent within an AI agent harness.

The Platform Agent acts as the master custodian and architect, responsible for multi-tenancy governance and cluster operations.

## Prerequisites

- An AI agent harness capable of running autonomous agents with workspace file access and tool execution capabilities.
- Kubernetes CLI (`kubectl`) configured with access to your target GKE clusters.
- **cert-manager** (v1.13.0+) installed on the target Kubernetes cluster for webhook TLS certificate management:
  - **Standard Installation (via Helm - Recommended)**:
    ```bash
    helm repo add jetstack https://charts.jetstack.io
    helm repo update
    helm install cert-manager jetstack/cert-manager \
      --namespace cert-manager \
      --create-namespace \
      --set installCRDs=true
    ```
  - **GKE Autopilot Installation (via Helm)**:
    GKE Autopilot blocks leader-election coordination Leases in the `kube-system` namespace. You must disable leader election during installation:
    ```bash
    helm repo add jetstack https://charts.jetstack.io
    helm repo update
    helm install cert-manager jetstack/cert-manager \
      --namespace cert-manager \
      --create-namespace \
      --set installCRDs=true \
      --set controller.leaderElection.enabled=false \
      --set cainjector.leaderElection.enabled=false
    ```
  - **Manifest-based Fallback (kubectl)**:
    If Helm is not available, you can apply the raw manifests directly:
    ```bash
    kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.14.4/cert-manager.yaml
    ```
    _Warning for GKE Autopilot_: If applying raw manifests, you must patch the deployments to append `--leader-elect=false` to their container arguments. Note that index-based JSON patching (e.g., modifying `/args/1`) is fragile and version-dependent; verify the argument list structure of the specific cert-manager version you install before patching.

## Installation Steps

### 1. Workspace Setup

The Platform Agent requires a dedicated workspace directory containing its core instructions, identity, and skills.

Copy the pre-packaged platform workspace to your agent harness's workspace directory:

```bash
# Assuming your agent harness workspace is at /path/to/harness/workspace/agents
cp -r agents/platform /path/to/harness/workspace/agents/platform
```

### 2. Agent Registration

Configure your AI agent harness to register a new agent named `platform`.

- **Workspace Directory**: Set the agent's workspace to the `platform` directory copied in Step 1.
- **System Prompt / Core Instructions**: Load the agent's primary instructions from `SOUL.md`.
- **Identity**: Load the agent's persona and constraints from `IDENTITY.md`.
- **Tools / Skills**: Ensure the agent has access to the skills defined in the `skills/` subdirectory.
- **Registration**: perform platform-specific agent registration (as required by your harness). If reload/restart is needed request user to perform the restart.

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

Once installed and the heartbeat is active, the Platform Agent will begin monitoring its state. You can interact with it directly to manage your Kubernetes clusters.
