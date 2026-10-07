---
title: Self-Managed Gitea Integration
description: Declaring a self-managed Gitea forge for in-cluster or enterprise GitOps repositories.
sidebar:
  order: 8
---

Kube-Agents supports self-managed **Gitea** instances (in-cluster or on-premises) as a first-class forge provider alongside GitHub. When declared, the Platform Agent autonomously reads repository context, opens GitOps pull requests, publishes branches, and posts review comments without external internet access or third-party SaaS dependencies.

In accordance with our platform invariants, Kube-Agents proposes GitOps changes as pull requests; it never mutates live cluster resources directly.

---

## Architecture & Credential Isolation

Gitea uses personal access tokens (PAT) stored in a Kubernetes Secret rather than the GitHub App installation token minter. To enforce [credential isolation](/kube-agents/reference/credential-isolation/), the secret is mounted exclusively into the `proxy` container of the `<agent-name>-cred-proxy` Deployment:

```text
Platform Agent Sandbox (No Tokens)
       │
       │ (HTTP RPC: /v1/vcs/proposal-create, /v1/vcs/publish, ...)
       ▼
Credential Proxy (deploy/<agent-name>-cred-proxy :8765)
       │ (Injects `Authorization: token <t>` & git http.extraHeader)
       ▼
Gitea Instance (In-Cluster Service or External FQDN)
```

The agent sandbox holds no Gitea tokens or API keys. Git operations in the sandbox use a credential-free working tree, while remote operations pass through the credential proxy, which scopes `http.<origin>.extraHeader` to the target forge origin.

---

## 1. Create the Token Secret

Generate a personal access token in your Gitea instance with `repo` and `issue` scopes, then store it in a Kubernetes Secret in the agent namespace:

```bash
kubectl -n kubeagents-system create secret generic gitea-token \
  --from-literal=token="YOUR_GITEA_PERSONAL_ACCESS_TOKEN"
```

The Secret key must be named `token`.

---

## 2. Declare the Forge in PlatformAgent CR

Configure `spec.integration.forges` with `provider: gitea`:

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: PlatformAgent
metadata:
  name: platform-agent
  namespace: kubeagents-system
spec:
  integration:
    forges:
      - name: in-cluster-gitea
        provider: gitea
        host: "gitea-http.gitea.svc.cluster.local"
        port: 3000
        scheme: http # Default is https; set http only for in-cluster clusterIP services
        namespace: demo
        credentialsRef:
          name: gitea-token
    repositories:
      - forge: in-cluster-gitea
        repository: gke-fleet-iac
        role: gitops
```

### Field Reference

| Field                 | Type    | Description                                                                                                  |
| :-------------------- | :------ | :----------------------------------------------------------------------------------------------------------- |
| `name`                | string  | DNS-compliant identifier for the forge (e.g. `in-cluster-gitea`).                                            |
| `provider`            | string  | Must be `gitea`.                                                                                             |
| `host`                | string  | Required. Hostname or in-cluster Service DNS (e.g. `gitea-http.gitea.svc.cluster.local`).                    |
| `port`                | integer | Optional port number (`1`–`65535`). Omit for standard `443`/`80`.                                            |
| `scheme`              | string  | `https` (default) or `http`.                                                                                 |
| `namespace`           | string  | Optional default organization or user namespace owning bare repository names on this forge (up to 40 chars). |
| `credentialsRef.name` | string  | Required. Name of the Secret holding the `token` key in the `PlatformAgent` namespace.                       |

---

## 3. Helm Values Configuration

When deploying Kube-Agents via Helm, configure `platformAgent.integration` in `values.yaml`:

```yaml
platformAgent:
  integration:
    forges:
      - name: enterprise-gitea
        provider: gitea
        host: "gitea.internal.net"
        scheme: https
        namespace: platform-team
        credentialsRef:
          name: gitea-token
    repositories:
      - forge: enterprise-gitea
        repository: gke-fleet-iac
        role: gitops
```

---

## 4. Verification

Verify that the operator accepts the configuration and registers the repository in the `<agent-name>-gitops-state` ConfigMap:

```bash
kubectl -n kubeagents-system get platformagent platform-agent -o wide
kubectl -n kubeagents-system get configmap platform-agent-gitops-state -o yaml
```

Check the credential proxy Deployment (`deploy/platform-agent-cred-proxy`, container `proxy`) to verify that the proxy is listening on `:8765`:

```bash
kubectl -n kubeagents-system logs deploy/platform-agent-cred-proxy -c proxy
```
