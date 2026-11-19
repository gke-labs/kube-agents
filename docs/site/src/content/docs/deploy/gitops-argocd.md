---
title: GitOps with ArgoCD
description: Standing up ArgoCD and Config Connector as the pull-based reconciler that applies what the Platform Agent proposes.
sidebar:
  order: 6
---

kube-agents proposes changes; it never applies them. The [declarative workflow](/kube-agents/concepts/declarative-workflow/) ends at a merged pull request, and something else has to turn that merge into live infrastructure. The [reference GitOps layout](https://github.com/gke-labs/kube-agents/tree/main/examples/gitops-repo) applies YAML from CI and, when you need Helm charts or workloads on a cluster created in the same pull request, recommends **Config Sync on every cluster, enrolled through Config Connector** ([Reconciler](https://github.com/gke-labs/kube-agents/tree/main/examples/gitops-repo#reconciler)).

This page covers a different choice end to end: **ArgoCD** as the reconciler, with **Config Connector (KCC)** provisioning Google Cloud resources from the same repo. Flux fits the same contract too. The fleet audit currently traces chart workloads back to their declarations only through an Argo CD `Application` or a Flux `HelmRelease`, not yet through a Config Sync `RootSync`, which is a reason to pick one of those two today. The setup has enough sharp edges to be worth writing down.

Nothing here is required by kube-agents. The agent is indifferent to which reconciler you run; it only needs one to exist.

## Push versus pull, and why it matters here

A push pipeline (a GitHub Action running `kubectl apply` on merge) is the obvious first thing to build, and it inverts the security model kube-agents is built around.

The agent is read-only against your clusters on purpose. If your actuator is a CI job, then the write credential lives **outside** the cluster, in a CI system, reachable by any workflow in the repo. You have moved the privilege rather than removed it. Worse, a push pipeline only acts at merge time — drift that appears an hour later stays until someone notices.

A pull reconciler inverts both. The write credential lives inside the cluster and never leaves it; CI needs no cloud access at all. And reconciliation is continuous, so drift is corrected rather than merely overwritten on the next merge.

```
Platform Agent → PR → human approves & merges → ArgoCD pulls → live
   ├─ cloud resources     → Config Connector → Google Cloud
   ├─ cluster registrations → ArgoCD itself
   └─ per-cluster workloads → the target cluster
```

## What you need first

- A **hub cluster** to run ArgoCD on. GKE **Standard** with Workload Identity enabled — Config Connector's add-on requires Standard, and Workload Identity is what makes the credential-free setup below possible. This can be the same cluster the Platform Agent runs on.
- `gcloud` authenticated as a project admin, and permission to create service accounts and project IAM bindings.
- Egress from the hub's nodes to `github.com`. Public nodes have it; a private cluster needs Cloud NAT.

Throughout, substitute your own `PROJECT_ID`, `REGION`, `HUB_CLUSTER`, and `ORG/REPO`.

## Install ArgoCD

Pin a release tag rather than tracking `stable`, and use `--server-side`:

```bash
kubectl create namespace argocd
kubectl apply -n argocd --server-side -f \
  https://raw.githubusercontent.com/argoproj/argo-cd/v3.4.6/manifests/install.yaml
kubectl -n argocd rollout status deploy/argocd-server --timeout=300s
```

`--server-side` is not optional. The `ApplicationSet` CRD carries an annotation larger than the client-side apply limit, and a plain `kubectl apply` fails on it.

To reach the UI: `kubectl port-forward svc/argocd-server -n argocd 8080:443`, then log in as `admin` with

```bash
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d
```

## Give ArgoCD read-only access to the repo

**Do not reuse Minty's GitHub App for this.** [Minty](/kube-agents/deploy/token-minter/) brokers `contents: write`, `pull_requests: write`, and `issues: write` so the agent can open PRs and publish audit ledgers. ArgoCD only ever reads. Sharing one App would hand the reconciler write access it has no use for, and hand the agent's credential path a second consumer. Both changes run against a design whose whole point is that proposing and applying are separate privileges.

Create a **second, org-owned GitHub App** with **Repository permissions → Contents: Read-only**, no webhook, installed on the GitOps repo only. Then:

```bash
kubectl -n argocd create secret generic gitops-repo \
  --from-literal=type=git \
  --from-literal=url=https://github.com/ORG/REPO.git \
  --from-literal=githubAppID=<APP_ID> \
  --from-literal=githubAppInstallationID=<INSTALLATION_ID> \
  --from-file=githubAppPrivateKey=<path/to/app-private-key.pem>
kubectl -n argocd label secret gitops-repo argocd.argoproj.io/secret-type=repository
```

The installation ID is not shown in the UI. Sign a JWT with the App's private key and call `GET https://api.github.com/app/installations`; the `id` on the installation for your org is the value.

Delete the downloaded `.pem` once the Secret exists. It is a long-lived credential, and it must never be committed.

Verify the connection reports `Successful` before continuing — against a private repo this is a real proof of auth, since an unauthenticated clone would fail.

## Reach every cluster without storing a single token

ArgoCD's default cluster registration embeds a bearer token or client certificate per cluster. On GKE you can avoid that entirely.

Grant a service account `roles/container.developer` at the **project** level, bind it to ArgoCD's Kubernetes service accounts through Workload Identity, and let the bundled `argocd-k8s-auth` exec plugin mint credentials on demand:

```bash
gcloud iam service-accounts create argocd-fleet --project PROJECT_ID \
  --display-name "ArgoCD fleet access"
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member="serviceAccount:argocd-fleet@PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/container.developer"
for KSA in argocd-application-controller argocd-server; do
  gcloud iam service-accounts add-iam-policy-binding \
    argocd-fleet@PROJECT_ID.iam.gserviceaccount.com --project PROJECT_ID \
    --member="serviceAccount:PROJECT_ID.svc.id.goog[argocd/$KSA]" \
    --role="roles/iam.workloadIdentityUser"
  kubectl -n argocd annotate serviceaccount $KSA \
    iam.gke.io/gcp-service-account=argocd-fleet@PROJECT_ID.iam.gserviceaccount.com --overwrite
done
kubectl -n argocd rollout restart statefulset argocd-application-controller
kubectl -n argocd rollout restart deploy argocd-server
```

Confirm it works before relying on it:

```bash
kubectl -n argocd exec argocd-application-controller-0 -- argocd-k8s-auth gcp
```

You want an `ExecCredential` containing a token. A `Permission 'iam.serviceAccounts.getAccessToken' denied` immediately after granting the binding is usually propagation lag — check with `gcloud iam service-accounts get-iam-policy` and retry rather than granting more.

The binding is at the **project** level deliberately. That is what lets the Platform Agent oversee _any_ cluster in the project, including clusters that do not exist yet: a cluster created next month needs no new IAM. Registering it becomes a single credential-free manifest holding only its API endpoint and public CA:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: cluster-CLUSTER_NAME
  namespace: argocd
  labels:
    argocd.argoproj.io/secret-type: cluster
    gitops-role: workload
type: Opaque
stringData:
  name: CLUSTER_NAME
  server: https://ENDPOINT
  config: |
    {
      "execProviderConfig": {
        "command": "argocd-k8s-auth",
        "args": ["gcp"],
        "apiVersion": "client.authentication.k8s.io/v1beta1"
      },
      "tlsClientConfig": { "caData": "BASE64_CLUSTER_CA" }
    }
```

Both values come from `gcloud container clusters describe CLUSTER_NAME --region REGION --format='value(endpoint)'` and `--format='value(masterAuth.clusterCaCertificate)'`. Because the file carries no credential, it is safe to commit — which means cluster registration itself becomes a reviewable pull request, and an `ApplicationSet` with a cluster generator selecting on `gitops-role: workload` can deploy to each registered cluster automatically.

### A cluster created in the same pull request

The endpoint and CA exist only once the cluster does, so a Secret in this form cannot ride in the pull request that creates its cluster. Workloads for that cluster wait for a second pull request. To register the cluster in the same one, address it through [Connect Gateway](https://docs.cloud.google.com/kubernetes-engine/enterprise/multicluster-management/gateway/using) instead, whose URL is built from values known up front:

1. Register the cluster to the fleet in the same pull request: the cluster's Terraform `fleet` block, or a Config Connector `GKEHubMembership` (the layout README's [Reconciler](https://github.com/gke-labs/kube-agents/blob/main/examples/gitops-repo/README.md#reconciler) section shows one).
2. Grant the ArgoCD service account `roles/gkehub.gatewayEditor`, alongside the cluster-access role above. ArgoCD applies on every sync, so `roles/gkehub.gatewayReader` is enough only for Applications it never syncs.
3. Point the Secret's `server` at the gateway and leave `caData` empty, as the kubeconfig `gcloud` generates for the gateway does.

```yaml
stringData:
  name: CLUSTER_NAME
  server: https://connectgateway.googleapis.com/v1/projects/PROJECT_NUMBER/locations/global/gkeMemberships/CLUSTER_NAME
  config: |
    {
      "execProviderConfig": {
        "command": "argocd-k8s-auth",
        "args": ["gcp"],
        "apiVersion": "client.authentication.k8s.io/v1beta1"
      },
      "tlsClientConfig": { "insecure": false, "caData": "" }
    }
```

The membership name defaults to the cluster name. Build the URL from the membership's actual location, not from this example: a `GKEHubMembership` sets it in `spec.location`, and Terraform reports it as `fleet[0].membership_location`. For a regional membership, both the host and the path carry the region: `https://REGION-connectgateway.googleapis.com/v1/projects/PROJECT_NUMBER/locations/REGION/gkeMemberships/CLUSTER_NAME`. This form is documented in a [Google Cloud blog post](https://cloud.google.com/blog/products/containers-kubernetes/connect-gateway-with-argocd) rather than in the Argo CD or GKE product docs, so test it on one cluster first. The [Connect Gateway page](https://docs.cloud.google.com/kubernetes-engine/enterprise/multicluster-management/gateway/using) also sets a per-project quota on active streams; whether ArgoCD's watches count against it is not documented. Config Sync, the reconciler the reference layout recommends when you need charts, needs neither the gateway nor this Secret.

## Provision cloud resources from the same repo

If you want the repo to describe GKE clusters and other Google Cloud resources — not just what runs inside them — add Config Connector on the hub. This is what backs the `provisioning/` path in the reference layout when you express it as KCC custom resources rather than Terraform.

```bash
gcloud container clusters update HUB_CLUSTER --region REGION \
  --project PROJECT_ID --update-addons ConfigConnector=ENABLED
```

Run KCC in **namespaced** mode so its permissions are scoped to one namespace rather than the whole cluster, and bind that namespace to its own service account. The KCC controller needs `roles/container.admin`, `roles/compute.viewer`, and `roles/iam.serviceAccountUser`.

The Workload Identity member string is the part most people get wrong. In namespaced mode the Kubernetes service account is `cnrm-controller-manager-<namespace>`, **not** the cluster-mode `cnrm-controller-manager`:

```bash
gcloud iam service-accounts add-iam-policy-binding \
  KCC_GSA@PROJECT_ID.iam.gserviceaccount.com --project PROJECT_ID \
  --member="serviceAccount:PROJECT_ID.svc.id.goog[cnrm-system/cnrm-controller-manager-NAMESPACE]" \
  --role="roles/iam.workloadIdentityUser"
```

Then apply a `ConfigConnector` in `namespaced` mode and a `ConfigConnectorContext` in your namespace naming that service account, and check `status.healthy` is `true` before trusting it.

The Platform Agent authors KCC manifests for this path through its [`gcp-config-connector` skill](https://github.com/gke-labs/kube-agents/blob/main/agents/platform/skills/gcp-config-connector/SKILL.md), which covers `ContainerCluster`/`ContainerNodePool`, `SQLInstance` and `ComputeFirewall`. It assumes three things about your installation, and stops with the missing one named rather than working around it:

- **The KCC CRDs are installed on a cluster the agent's kubeconfig reaches.** Its only pre-PR validation is `kubectl explain` against those CRDs; there is no bundled schema, and the agent cannot run `kubectl apply --dry-run=server` (write RBAC it does not hold, and a verb its command policy refuses). That dry-run is the reviewer's or CI's step, and the agent says so in every PR body.
- **The namespace it writes into is bound to a project** — through `ConfigConnectorContext` or a `cnrm.cloud.google.com/project-id` annotation on the sibling manifests, which it copies rather than choosing.
- **Read access to the `cnrm.cloud.google.com` API groups** decides create versus acquire. On the management cluster reached in-cluster, the Kubernetes ClusterRole bounds that read and grants none of those groups; on a separate hub reached through `gcloud container clusters get-credentials`, the Google service account's permission set decides instead. [Security &amp; IAM](/kube-agents/reference/security-and-iam/) is canonical for both. Where the read is refused, the agent reports the `Forbidden` and asks whether the resource exists instead of guessing.

## Make deletion hard on purpose

Auto-sync with prune is right for workloads: remove a manifest, the object goes away, and you can recreate it. It is wrong for a GKE cluster.

Two independent brakes give you a safe default:

- **`Prune=confirm`** on the Application that syncs cloud resources. Removing a manifest then _stages_ a deletion and waits — the Application sits out of sync, showing the prune it wants to perform, until a human approves it in the UI or with `argocd app confirm-deletion <app>`.
- **`cnrm.cloud.google.com/deletion-policy: abandon`** on every KCC resource. Even a confirmed prune then only detaches KCC and leaves the cloud resource running.

Together, destroying a cluster takes two deliberate acts that cannot happen by accident: a reviewed PR flipping the annotation to `delete`, and then a confirmed prune. Leave workload Applications on plain auto-prune; reserve the brakes for the path that touches cloud resources.

One related note on the KCC annotation `cnrm.cloud.google.com/state-into-spec`. Under `merge`, Config Connector writes the live values of every field a manifest omits back into the object's `spec`. ArgoCD then sees an object that no longer matches git and reports the Application out of sync for as long as the annotation stands, and with self-heal on it re-applies against KCC's writes. Set it to `absent` on every KCC resource, acquired or created, so omitted fields stay externally managed and the file stays the source of truth. It is immutable once set, so it goes on the first commit; the agent's skill writes it on every manifest it authors.

## Auto-merge and this pipeline

If you have auto-merge on agent-authored PRs, understand what ArgoCD changes about it: nothing, and that is the problem. Under a push pipeline, an auto-merged PR already reached the cluster without a human. Under ArgoCD it still does — but now the same repo can also provision cloud resources.

Scope the auto-merge guard by path so that PRs touching cloud resources, cluster registrations, or the bootstrap manifests always require a person. Registering a cluster grants the reconciler write access to it, so it needs a person's approval.

## Deploying kube-agents via Argo CD without Helm lookup

When you deploy the `kube-agents` Helm chart through Argo CD with external secrets (`platformAgent.credentials.create: false`), one chart Secret is omitted from the render. Understanding why requires looking at how the shell sandbox receives its SSH public key.

When `platformAgent.credentials.create` is `false`, `templates/platform-agent-shell-authorized-keys.yaml` reads `SANDBOX_SSH_PUBLIC_KEY` from `platform-agent-secrets` using Helm's `lookup` function. Because Argo CD renders charts offline with `helm template`, `lookup` returns an empty map and the chart omits `<platformAgent.name>-shell-authorized-keys`. Without that Secret, the operator sets `Degraded` (`ShellSandboxKeysMissing`) on the `PlatformAgent` resource and `<name>-shell-0` stays in `ContainerCreating`.

The chart keeps `<platformAgent.name>-shell-authorized-keys` separate from `platform-agent-secrets` to preserve the sandbox boundary. The `<name>-shell` StatefulSet runs model-authored shell commands and mounts only `<platformAgent.name>-shell-authorized-keys` (key `authorized_keys`) at `/etc/ssh-authorized`. It never mounts `platform-agent-secrets`, so `SANDBOX_SSH_PRIVATE_KEY`, `API_SERVER_KEY`, `SESSION_KV_API_KEY`, `SESSION_KV_SALT`, model keys, and chat tokens stay out of the sandbox pod.

### Required and optional Secrets

Keep `platformAgent.credentials.secretName` at its default (`platform-agent-secrets`), because the operator reads `SANDBOX_SSH_PRIVATE_KEY`, `SESSION_KV_API_KEY`, and `SESSION_KV_SALT` from `platform-agent-secrets` by name. Provision both required Secrets in `kubeagents-system` before or alongside the Argo CD `Application`:

| Secret / Helm value                                                                   | Keys                                                                                 | Required    | Consumer and purpose                                                                                                                                     |
| ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ | ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `platform-agent-secrets` (`platformAgent.credentials.secretName`)                     | `API_SERVER_KEY`                                                                     | Required    | Bearer key for the Platform Agent API proxy (`agent-api-auth` sidecar) on `<name>-gateway`.                                                              |
| `platform-agent-secrets`                                                              | `SESSION_KV_API_KEY`                                                                 | Required    | Bearer token for the pod-local Session KV server; when absent, `k8s-event-watcher` exits on startup and Session KV returns `503`.                        |
| `platform-agent-secrets`                                                              | `SESSION_KV_SALT`                                                                    | Required    | HMAC salt for pseudonymising chat identities and redaction tokens; preserve across upgrades so user sessions stay linked.                                |
| `platform-agent-secrets`                                                              | `SANDBOX_SSH_PRIVATE_KEY`                                                            | Required    | OpenSSH `ed25519` private key staged by the `sandbox-ssh-key` init container on `<name>-gateway` at `/etc/sandbox-ssh/id_ed25519`.                       |
| `platform-agent-secrets`                                                              | `SANDBOX_SSH_PUBLIC_KEY`                                                             | Required    | OpenSSH `ed25519` public key (`authorized_keys` line) paired with `SANDBOX_SSH_PRIVATE_KEY` for live Helm upgrades and `upgrade.sh`.                     |
| `platform-agent-secrets`                                                              | `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENAI_COMPATIBLE_API_KEY` | Conditional | Model provider API key matching `litellm.modelProvider` (`gemini`, `anthropic`, `openai`, or `openai_compatible`); omit for `vertex_ai` on Workload IAM. |
| `platform-agent-secrets`                                                              | `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `TEAMS_APP_ID`, `TEAMS_APP_PASSWORD`           | Optional    | Chat adapter credentials when not stored in separate Secrets referenced via `*SecretRef`.                                                                |
| `<platformAgent.name>-shell-authorized-keys` (`platform-agent-shell-authorized-keys`) | `authorized_keys`                                                                    | Required    | Single public-key line matching `SANDBOX_SSH_PUBLIC_KEY`, mounted read-only into `<name>-shell` at `/etc/ssh-authorized/authorized_keys`.                |
| `platformAgent.integration.slack.botTokenSecretRef`, `appTokenSecretRef`              | Custom `{name, key}`                                                                 | Optional    | Separate Secret references for the Slack bot (`xoxb-...`) and Socket Mode app (`xapp-...`) tokens.                                                       |
| `platformAgent.integration.teams.appIdSecretRef`, `appPasswordSecretRef`              | Custom `{name, key}`                                                                 | Optional    | Separate Secret references for the Microsoft Teams adapter application ID and password.                                                                  |
| `platformAgent.integration.forges[].credentialsRef`                                   | `token` (plus optional `read-token` for `bitbucket`)                                 | Optional    | Per-forge Secret for `gitlab`, `gitea`, `bitbucket`, or `bitbucket-datacenter`, mounted only into `<name>-credential-proxy`.                             |
| `litellm.envFromSecret`                                                               | Provider-specific env vars                                                           | Optional    | Extra Secret loaded via `envFrom` into `litellm` (required when `litellm.modelProvider` is `sap`, or for custom LiteLLM provider credentials).           |

### ExternalSecret and Argo CD Application manifests

When you manage Secrets with External Secrets Operator, project the public key into both `platform-agent-secrets` and `platform-agent-shell-authorized-keys`. That keeps the private key and application tokens restricted to `platform-agent-secrets`:

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: platform-agent-secrets
  namespace: kubeagents-system
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: ClusterSecretStore
    name: gcp-secret-manager
  target:
    name: platform-agent-secrets
    creationPolicy: Owner
  data:
    - secretKey: API_SERVER_KEY
      remoteRef:
        key: kubeagents-api-server-key
    - secretKey: SESSION_KV_API_KEY
      remoteRef:
        key: kubeagents-session-kv-api-key
    - secretKey: SESSION_KV_SALT
      remoteRef:
        key: kubeagents-session-kv-salt
    - secretKey: SANDBOX_SSH_PRIVATE_KEY
      remoteRef:
        key: kubeagents-sandbox-ssh-private-key
    - secretKey: SANDBOX_SSH_PUBLIC_KEY
      remoteRef:
        key: kubeagents-sandbox-ssh-public-key
---
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: platform-agent-shell-authorized-keys
  namespace: kubeagents-system
spec:
  refreshInterval: 1h
  secretStoreRef:
    kind: ClusterSecretStore
    name: gcp-secret-manager
  target:
    name: platform-agent-shell-authorized-keys
    creationPolicy: Owner
  data:
    - secretKey: authorized_keys
      remoteRef:
        key: kubeagents-sandbox-ssh-public-key
```

In the Argo CD `Application`, set `platformAgent.credentials.create: false` and explicitly set the monitoring flags your cluster uses. Those monitoring flags default to `null` (auto-detect via `.Capabilities.APIVersions.Has`), which evaluates to false during an offline `helm template` render unless set explicitly:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: kube-agents
  namespace: argocd
spec:
  project: default
  source:
    repoURL: ghcr.io/gke-labs/kube-agents/charts
    chart: kube-agents
    targetRevision: X.Y.Z
    helm:
      valuesObject:
        platformAgent:
          name: platform-agent
          credentials:
            create: false
            secretName: platform-agent-secrets
          harness:
            clusterName: HUB_CLUSTER
            location: REGION
            projectId: PROJECT_ID
          podMonitoring: true
          serviceMonitor: false
          vmPodScrape: false
        operator:
          metrics:
            podMonitoring: true
        litellm:
          modelProvider: vertex_ai
          podMonitoring: true
        hindsight:
          podMonitoring: false
  destination:
    server: https://kubernetes.default.svc
    namespace: kubeagents-system
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
      - ServerSideApply=true
```

### Rotating the sandbox SSH keypair

Neither `<name>-shell` nor `<name>-gateway` reloads the sandbox SSH keypair from its mounted Secret volume while running. In `<name>-shell`, `entrypoint.sh` copies `/etc/ssh-authorized/authorized_keys` into `/home/agent/.ssh/authorized_keys` and `/home/hermes/.ssh/authorized_keys` once before starting `sshd`. In `<name>-gateway`, the `sandbox-ssh-key` init container copies `SANDBOX_SSH_PRIVATE_KEY` to `/etc/sandbox-ssh/id_ed25519` once at pod startup.

Rotate the keypair in order so `<name>-shell` accepts the new public key before `<name>-gateway` dials with the new private key:

1. Generate a fresh `ed25519` keypair on your workstation:

   ```bash
   ssh-keygen -t ed25519 -N "" -C "platform-agent@kubeagents-system" -f ./sandbox_id_ed25519
   ```

2. Write `./sandbox_id_ed25519.pub` to `authorized_keys` in `<name>-shell-authorized-keys` and `SANDBOX_SSH_PUBLIC_KEY` in `platform-agent-secrets`, and write `./sandbox_id_ed25519` to `SANDBOX_SSH_PRIVATE_KEY` in `platform-agent-secrets`. If you use `ExternalSecret`, update the backing Secret Manager versions and force or wait for both Kubernetes Secrets to sync:

   ```bash
   kubectl -n kubeagents-system annotate externalsecret \
     platform-agent-secrets platform-agent-shell-authorized-keys \
     force-sync="$(date +%s)" --overwrite
   ```

3. Restart `statefulset/<name>-shell` first and wait for it to become ready so `sshd` loads the new `authorized_keys`:

   ```bash
   kubectl -n kubeagents-system rollout restart statefulset/platform-agent-shell
   kubectl -n kubeagents-system rollout status statefulset/platform-agent-shell --timeout=180s
   ```

4. Restart `deployment/<name>-gateway` second so the `sandbox-ssh-key` init container stages the new `id_ed25519` private key:

   ```bash
   kubectl -n kubeagents-system rollout restart deployment/platform-agent-gateway
   kubectl -n kubeagents-system rollout status deployment/platform-agent-gateway --timeout=180s
   ```

5. Delete the temporary keypair files from your workstation:

   ```bash
   rm -f ./sandbox_id_ed25519 ./sandbox_id_ed25519.pub
   ```

## Gotchas

- **`kubectl apply` fails on the ApplicationSet CRD.** Its annotation exceeds the client-side limit. Use `--server-side`.
- **Namespaced KCC uses a per-namespace service account.** Binding Workload Identity to the cluster-mode name yields a controller that starts cleanly and then 403s on every reconcile — a failure that looks like an IAM problem but is a name problem.
- **A `ConfigConnector` resource cannot be deleted after its operator is gone.** Disabling the add-on first leaves the finalizer with nothing to process it. Patch `metadata.finalizers` to `[]`, then delete.
- **`container.admin` alone is not enough for KCC.** Reading a cluster's node-pool instance groups needs `compute.viewer`, or you get a `compute.instanceGroupManagers.get` 403.
- **Creating a cluster, as opposed to adopting one, needs `iam.serviceAccountUser`** on the default compute service account.
- **Kubernetes' RBAC privilege-escalation check ignores the GKE IAM authorizer.** ArgoCD's IAM permissions let it create `Role` objects, but syncing a Role that _delegates_ a permission fails unless the ArgoCD identity already holds that permission through **Kubernetes** RBAC. A workload defining a Role that grants `secrets` will not sync until you grant those verbs natively, out of band — ArgoCD cannot self-apply that grant, since doing so is itself an escalation.
- **IAM propagation lags.** Auth failures in the first minute or two after a binding are usually timing. Verify the policy, then retry.

## Where to go next

- [Concepts → Declarative workflow](/kube-agents/concepts/declarative-workflow/) — what the agent puts into the repo, and why it never applies it.
- [Deploy → Token minter](/kube-agents/deploy/token-minter/) — the write-side credential path, and why it stays separate from the one above.
- [Reference → Security &amp; IAM](/kube-agents/reference/security-and-iam/) — what the agent is and is not permitted to do.
- [Reference GitOps layout](https://github.com/gke-labs/kube-agents/tree/main/examples/gitops-repo) — the repository structure this reconciler applies.
