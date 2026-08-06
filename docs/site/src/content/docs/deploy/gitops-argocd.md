---
title: GitOps with ArgoCD
description: Standing up ArgoCD and Config Connector as the pull-based reconciler that applies what the Platform Agent proposes.
sidebar:
  order: 6
---

kube-agents proposes changes; it never applies them. The [declarative workflow](/kube-agents/concepts/declarative-workflow/) ends at a merged pull request, and something else has to turn that merge into live infrastructure. The [reference GitOps layout](https://github.com/gke-labs/kube-agents/tree/main/examples/gitops-repo) calls that something "the customer's CI/CD" and leaves the choice to you.

This page covers one such choice end to end: **ArgoCD** as the reconciler, with **Config Connector (KCC)** provisioning Google Cloud resources from the same repo. It is not the only option — Flux and Config Sync fit the same contract — but it is the one the agent's own tooling is most often pointed at, and the setup has enough sharp edges to be worth writing down.

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

**Do not reuse Minty's GitHub App for this.** [Minty](/kube-agents/deploy/token-minter/) brokers `contents: write`, `pull_requests: write`, and `issues: write` so the agent can open PRs and publish audit ledgers. ArgoCD only ever reads. Sharing one App would hand the reconciler write access it has no use for, and hand the agent's credential path a second consumer — two changes in the wrong direction for a design whose whole point is that proposing and applying are separate privileges.

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

The binding is at the **project** level deliberately. That is what lets the Platform Agent oversee _any_ cluster in the project, including clusters that do not exist yet: a cluster created next month is already reachable, with no new IAM. Registering it becomes a single credential-free manifest holding only its API endpoint and public CA:

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

## Make deletion hard on purpose

Auto-sync with prune is right for workloads: remove a manifest, the object goes away, and you can recreate it. It is wrong for a GKE cluster.

Two independent brakes give you a safe default:

- **`Prune=confirm`** on the Application that syncs cloud resources. Removing a manifest then _stages_ a deletion and waits — the Application sits out of sync, showing the prune it wants to perform, until a human approves it in the UI or with `argocd app confirm-deletion <app>`.
- **`cnrm.cloud.google.com/deletion-policy: abandon`** on every KCC resource. Even a confirmed prune then only detaches KCC and leaves the cloud resource running.

Together, destroying a cluster takes two deliberate acts that cannot happen by accident: a reviewed PR flipping the annotation to `delete`, and then a confirmed prune. Leave workload Applications on plain auto-prune; reserve the brakes for the path that touches cloud resources.

One related note on the KCC annotation `cnrm.cloud.google.com/state-into-spec: merge` — it exists for _acquiring_ resources that already exist, and it is immutable once set. Put it on resources you adopt; leave it off resources the repo creates.

## Auto-merge and this pipeline

If you have auto-merge on agent-authored PRs, understand what ArgoCD changes about it: nothing, and that is the problem. Under a push pipeline, an auto-merged PR already reached the cluster without a human. Under ArgoCD it still does — but now the same repo can also provision cloud resources.

Scope the auto-merge guard by path so that PRs touching cloud resources, cluster registrations, or the bootstrap manifests always require a person. Registering a cluster grants the reconciler write access to it; that is a human decision, not a lint check.

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
