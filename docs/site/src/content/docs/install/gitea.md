---
title: Gitea as the GitOps forge
description: Declaring a self-managed Gitea forge for in-cluster or enterprise GitOps repositories — the token scopes, private CA bundle, and Helm/CRD configuration.
---

Kube-Agents supports self-managed **Gitea** instances (in-cluster or on-premises) as a first-class forge provider alongside GitHub and GitLab. When declared, the Platform Agent autonomously reads repository context, opens GitOps pull requests, publishes branches, and posts review comments without external internet access or third-party SaaS dependencies.

In accordance with our platform invariants, Kube-Agents proposes GitOps changes as pull requests; it never mutates live cluster resources directly.

---

## Architecture & Credential Isolation

Gitea uses access tokens stored in a Kubernetes Secret rather than the GitHub App installation token minter. To enforce [credential isolation](/kube-agents/reference/credential-isolation/), the Secret is mounted exclusively into the `envoy-credential-proxy` container of the `<agent-name>-credential-proxy` Deployment at `/var/run/kube-agents/forge-credentials/<forge-name>/token`, and the operator renders the forge configuration (`VCS_FORGES_CONFIG`) at `/etc/credential-proxy/vcs-forges.json`:

```text
Platform Agent Sandbox (No Tokens)
       │
       │ (HTTP RPC: /v1/vcs/proposal-create, /v1/vcs/publish, ...)
       ▼
Credential Proxy (deploy/<agent-name>-credential-proxy :8765)
       │ (Injects `Authorization: token <t>` & `credential.<origin>.helper`)
       ▼
Gitea Instance (In-Cluster Service or Self-Managed FQDN)
```

The agent sandbox holds no Gitea tokens or API keys. Git operations in the sandbox use a credential-free working tree, while remote operations pass through the credential proxy, which scopes the token-file credential helper (`credential.<origin>.helper`) and `allowedPaths` to the target forge origin.

---

## 1. Create the Token Secret

Generate an access token in your Gitea instance (from a dedicated bot/automation account, never a person's own account) with the following Gitea 1.19+ scopes:

- `write:repository` — clone, fetch, push branches, and open/update pull requests.
- `write:issue` — list, read, comment on, and label issues and pull requests.
- `read:user` — resolve the authenticated bot login (`GET /api/v1/user`) and enumerate reachable repositories (`GET /api/v1/user/repos`).

Store the token in a Kubernetes Secret in the agent's namespace under the key `token` using `--from-file` and `--server-side` so the token never appears in process arguments or `kubectl.kubernetes.io/last-applied-configuration` annotations:

```bash
kubectl create secret generic gitea-forge-token -n kubeagents-system \
  --from-file=token="$HOME/secure/gitea-token" --dry-run=client -o yaml \
  | kubectl apply --server-side --force-conflicts -f -
```

The broker reads the mounted file on every call, so rotating the token requires only updating the Secret.

### Optional: Private CA Bundle (`caBundleRef`)

For a self-managed HTTPS Gitea instance whose TLS certificate is signed by a private CA, store the PEM certificate chain in a Secret under the key `ca.crt`:

```bash
kubectl create secret generic gitea-forge-ca -n kubeagents-system \
  --from-file=ca.crt="$HOME/secure/internal-ca.pem" --dry-run=client -o yaml \
  | kubectl apply --server-side --force-conflicts -f -
```

Reference it via `caBundleRef` on the forge entry. The operator mounts the CA bundle into the credential broker at `/etc/kube-agents/forge-ca/<forge-name>/ca.crt`, and the broker trusts it for that forge's host only. (`caBundleRef` is only valid when `scheme` is `https`.)

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
        scheme: http # Default is https; http is only permitted for in-cluster .svc / .svc.cluster.local hosts
        namespace: demo
        credentialsRef:
          name: gitea-forge-token
    repositories:
      - forge: in-cluster-gitea
        repository: gke-fleet-iac
        role: gitops
```

### Field Reference

| Field                 | Type    | Description                                                                                                        |
| :-------------------- | :------ | :----------------------------------------------------------------------------------------------------------------- |
| `name`                | string  | DNS-compliant identifier for the forge (e.g. `in-cluster-gitea`).                                                  |
| `provider`            | string  | Must be `gitea`.                                                                                                   |
| `host`                | string  | Required. Self-managed hostname or in-cluster Service DNS (e.g. `gitea-http.gitea.svc.cluster.local`).             |
| `port`                | integer | Optional port number (`1`–`65535`). Omit for standard `443`/`80`.                                                  |
| `scheme`              | string  | `https` (default) or `http`. `http` is restricted to in-cluster `.svc` and `.svc.cluster.local` Service DNS names. |
| `namespace`           | string  | Optional default organization or user namespace owning bare repository names on this forge (up to 40 chars).       |
| `credentialsRef.name` | string  | Required. Name of the Secret holding the `token` key in the `PlatformAgent` namespace.                             |
| `caBundleRef.name`    | string  | Optional (`https` only). Name of the Secret holding the PEM CA bundle (`key` defaults to `ca.crt`).                |

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
          name: gitea-forge-token
        caBundleRef:
          name: gitea-forge-ca
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

Check the credential proxy Deployment (`deploy/platform-agent-credential-proxy`, container `envoy-credential-proxy`) to verify that the proxy is listening on `:8765`:

```bash
kubectl -n kubeagents-system logs deploy/platform-agent-credential-proxy -c envoy-credential-proxy
```
