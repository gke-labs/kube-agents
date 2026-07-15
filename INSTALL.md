# Kubernetes Agentic Harness (`kube-agents`) Installation Guide

This guide explains how to install and configure agent deployment profiles within the `kube-agents` harness.

`kube-agents` supports two primary deployment profiles:
1. **Platform Agent (`platform`)**: Default full-capability master custodian for GKE operations and multi-tenancy governance.
2. **Read-Only SRE Agent (`sre-readonly`)**: Secure-by-design, diagnostic-only partner for alert triage, root-cause investigation, and Git PR remediation.

---

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

---

## Profile Selection & Installation Steps

### Option A: Read-Only SRE Agent (`sre-readonly`) [Recommended for Production Safety]

The Read-Only SRE Agent operates under strict read-only Kubernetes RBAC rules and outputs fixes via Git Pull Requests.

#### 1. Workspace Setup
Copy the `sre-readonly` blueprint to your agent harness workspace:
```bash
cp -r agents/sre-readonly /path/to/harness/workspace/agents/sre-readonly
```

#### 2. Deploying to Cluster via Operator
Deploy the `PlatformAgent` Custom Resource configured with the `sre-readonly-agent` image target and read-only RBAC:

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: PlatformAgent
metadata:
  name: sre-readonly-agent
  namespace: kubeagents-system
spec:
  deployment:
    image: "ghcr.io/gke-labs/kube-agents/sre-readonly-agent"
    tag: "latest"
  security:
    serviceAccountName: "kubeagents-sre-readonly"
  integration:
    github:
      gitRepo: "owner/your-gitops-repo"
```

Apply the strict read-only RBAC manifest:
```bash
kubectl apply -f k8s-operator/config/agent_rbac/sre_readonly_agent.yaml
kubectl apply -f k8s-operator/examples/sre-readonly-agent.yaml
```

---

### Option B: Platform Agent (`platform`) [Default Full Capability]

The Platform Agent manages infrastructure lifecycles, CRDs, and cluster multi-tenancy.

#### 1. Workspace Setup
Copy the `platform` blueprint to your agent harness workspace:
```bash
cp -r agents/platform /path/to/harness/workspace/agents/platform
```

#### 2. Agent Registration
Configure your harness to register the agent named `platform`:
- **Workspace Directory**: Set to `agents/platform`.
- **System Prompt**: `SOUL.md`.
- **Skills**: `agents/platform/skills/`.

#### 3. Heartbeat Schedule Configuration
Configure a recurring scheduled cron task (`* * * * *`) for `platform`:
```text
[Scheduled Heartbeat]
Read HEARTBEAT.md and execute due checks.
Update memory/heartbeat-state.json with fresh timestamps/results.
If healthy and no anomalies, respond exactly NO_REPLY; otherwise return concise blockers.
```

---

## Post-Installation Verification

Check the deployment status in Kubernetes:
```bash
kubectl get platformagents -n kubeagents-system
kubectl get pods -n kubeagents-system
```
Once healthy (`Ready` phase), the agent will actively monitor cluster health and respond to alerts or direct queries according to its profile constraints.
