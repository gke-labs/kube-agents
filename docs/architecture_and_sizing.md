# Kube-Agents Architecture & Sizing Guide

This document provides a conceptual overview of the Kube-Agents system, its software inventory, deployment topology recommendations, and cluster sizing guidelines.

---

## Conceptual Overview

If you are new to Kubernetes or GKE, here is a brief overview of how the Kube-Agents system is structured:

- **The Agent Harness (Local/Host)**: The environment where the AI agent's reasoning logic runs (typically your local machine or a dedicated gateway service). It reads instructions from this workspace and executes tools.
- **The Kubernetes Cluster**: The managed environment (like GKE) where your application workloads run. Kube-Agents deploys an **Operator** onto this cluster to manage the agents that monitor it.
- **The Operator**: A controller running inside your cluster that manages the lifecycle of the agents.
- **Cert-Manager**: Automatically manages SSL/TLS certificates for secure webhook communication within the cluster.
- **GKE Autopilot vs. Standard**:
  - _Standard_ gives you full control over the underlying nodes.
  - _Autopilot_ is fully managed by Google and restricts certain high-privilege operations (like leader election in `kube-system`). The installation guide includes workarounds for these restrictions.

### Architecture Diagram

The following diagram illustrates the relationship between the hub GKE cluster running the agent harness, the target cluster fleet (spokes), and the secure GitOps write path:

```mermaid
graph TB
    subgraph Hub ["Management GKE Cluster (Hub)"]
        subgraph Namespace ["Namespace: kubeagents-system"]
            Operator["Kube-Agents Operator"]
            AgentPod["Platform Agent Pod"]
            LiteLLM["LiteLLM Gateway"]
            Minty["GitHub Minter"]
        end
        Operator -->|Manages| AgentPod
        AgentPod -->|"1. Prompts"| LiteLLM
        AgentPod -->|"2. Auth Request"| Minty
    end

    subgraph External ["External Services"]
        Gemini["Gemini API"]
        GitHub["GitHub (GitOps Repo)"]
        GChat["Google Chat / Slack"]
    end

    LiteLLM -->|"3. API Request"| Gemini
    GChat -->|"Event Stream / User Input"| AgentPod

    subgraph Spoke1 ["Target Cluster 1 (Spoke)"]
        APIServer1["GKE API Server 1"]
        Argo1["GitOps Controller (Argo/Flux)"]
        Workloads1["Workloads"]
        Argo1 -->|"Deploy"| APIServer1
        APIServer1 -->|"Run"| Workloads1
    end

    subgraph Spoke2 ["Target Cluster 2 (Spoke)"]
        APIServer2["GKE API Server 2"]
        Argo2["GitOps Controller (Argo/Flux)"]
        Workloads2["Workloads"]
        Argo2 -->|"Deploy"| APIServer2
        APIServer2 -->|"Run"| Workloads2
    end

    %% Direct Control (Default / Admin Mode)
    AgentPod -->|"Direct gcloud/kubectl"| APIServer1
    AgentPod -->|"Direct gcloud/kubectl"| APIServer2

    %% GitOps Write Path (Secure / Read-Only Mode)
    AgentPod -->|"Git Push / PR"| GitHub
    Minty -->|"Mint Token"| GitHub
    GitHub -->|"Reconcile"| Argo1
    GitHub -->|"Reconcile"| Argo2
```

---

## Software Inventory

When you deploy `kube-agents`, the following components are installed on your Kubernetes cluster:

| Component                       | Origin                  | Purpose                                                    | Default Namespace                |
| :------------------------------ | :---------------------- | :--------------------------------------------------------- | :------------------------------- |
| **Kube-Agents Operator**        | Custom (Go)             | Manages the lifecycle of Agent custom resources.           | `kubeagents-system`              |
| **Platform Agent Pod**          | Custom (Python/Hermes)  | The active AI agent runtime environment.                   | `kubeagents-system`              |
| **LiteLLM Gateway**             | 3rd Party (Open Source) | Proxies LLM requests, manages API keys, and logs usage.    | `kubeagents-system`              |
| **GitHub Token Minter (Minty)** | Custom (Go)             | Securely mints temporary GitHub tokens using KMS.          | `kubeagents-system`              |
| **Fluent Bit**                  | 3rd Party (Open Source) | Forward logs from the agent pod to Cloud Logging.          | `kubeagents-system` (as sidecar) |
| **cert-manager**                | 3rd Party (Open Source) | Manages TLS certificates for secure webhook communication. | `cert-manager`                   |

---

## Deployment Topology: Shared vs. Dedicated Cluster

- **Permissions**: The **Platform Agent** requires broad cluster-level permissions (RBAC) to manage namespaces, deployments, and service accounts.
- **Security Implications**: Because of these broad permissions, running Kube-Agents on a **shared production cluster** with other critical workloads poses a security risk if the agent's execution sandbox is compromised.
- **Recommendation**:
  - **Development/Testing**: A shared cluster is fine, using the `kubeagents-system` namespace for logical isolation.
  - **Production**: It is highly recommended to run Kube-Agents on a **dedicated cluster** to maintain a hard security boundary.

---

## Capacity Planning & Sizing Guidelines

Use the following guidelines to size your GKE cluster based on the number of target resources you expect the agent to manage:

| Fleet Size | GKE Node Configuration (Recommended)                          | Target Capacity                                                  | Notes                                                                    |
| :--------- | :------------------------------------------------------------ | :--------------------------------------------------------------- | :----------------------------------------------------------------------- |
| **Small**  | 1x `e2-standard-4` (4 vCPU, 16GB RAM)                         | Local cluster only + up to 10 namespaces.                        | GKE Standard or GKE Autopilot works well here.                           |
| **Medium** | 3x `e2-standard-4` or `e2-standard-8`                         | Local cluster + up to 5 remote clusters (100+ namespaces total). | Good for managing a small staging/dev fleet.                             |
| **Large**  | 3x `c2-standard-8` (Compute Optimized) + dedicated node pools | 10+ remote clusters (1000+ namespaces/workloads).                | Requires GKE Sandbox (gVisor) enabled for agent pod execution isolation. |
