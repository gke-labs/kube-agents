---
title: Enterprise Helm & GitOps
description: Deploy the kube-agents operator and Helm chart through Flux or Argo CD with separate Google Cloud IAM and an existing cert-manager.
---

Enterprise GKE teams often provision Google Cloud IAM in a dedicated Terraform pipeline and deploy Kubernetes workloads through Flux or Argo CD. This guide deploys the official [`kube-agents` Helm chart](https://github.com/gke-labs/kube-agents/tree/main/charts/kube-agents) and operator onto an existing GKE cluster without running `install.sh`.

This path installs the full operator, inference gateway, A2A stack, and `PlatformAgent` custom resource from the published OCI chart. If you instead want to copy the raw `agents/platform/` workspace files into your own Hermes runtime without the operator or chart, see [Manual install](/kube-agents/install/manual/).

## Cluster and project prerequisites

Your target GKE cluster must meet the [Cluster requirements](/kube-agents/install/prerequisites/#cluster-requirements): GKE 1.29 or newer, Workload Identity (`<PROJECT_ID>.svc.id.goog`) with `GKE_METADATA` on every node pool, NetworkPolicy enforcement via Dataplane V2 or Calico, and [cert-manager](/kube-agents/install/prerequisites/#cert-manager-on-the-target-cluster) v1.13.0+ for the operator's admission webhook certificates.

The chart runs the agent under the `gvisor` RuntimeClass by default (included on GKE Autopilot, or via a sandboxed node pool on GKE Standard). To run on the standard container runtime instead, set `platformAgent.deployment.availability.runtimeClassName: ""` in your Helm values. Deploy into the `kubeagents-system` namespace so the release matches the default Terraform Workload Identity bindings and the image's baked fallback gateway address.

## Pipeline 1: Google Cloud IAM and infrastructure (Terraform)

You can provision the Google Cloud service accounts, IAM bindings, Pub/Sub topics, and Cloud KMS keys in two ways. Either run [`terraform/examples/full-install`](https://github.com/gke-labs/kube-agents/tree/main/terraform/examples/full-install) in infrastructure-only mode, or compose the individual modules under [`terraform/modules/`](https://github.com/gke-labs/kube-agents/tree/main/terraform/modules) in your own Terraform root.

### Option A: `full-install` with `install_helm_releases = false`

Setting `install_helm_releases = false` tells [`terraform/examples/full-install`](https://github.com/gke-labs/kube-agents/tree/main/terraform/examples/full-install) to provision only the Google Cloud resources and skip both `helm_release` resources. Terraform never connects to the GKE control plane in this mode, so your cloud pipeline needs no network path to a private cluster endpoint.

Pair `install_helm_releases = false` with `create_cluster = false`, `enable_cert_manager = false`, your release `image_tag`, and `agent_runtime_class = "gvisor"` in `terraform.tfvars`:

```hcl
project_id            = "my-gcp-project"
cluster_name          = "my-existing-cluster"
location              = "us-central1"
create_cluster        = false
enable_cert_manager   = false
install_helm_releases = false

image_tag             = "<RELEASE_VERSION>"
agent_runtime_class   = "gvisor" # set "" for the standard container runtime

# Model provider: vertex_ai uses Workload Identity with no API key.
model_provider        = "vertex_ai"
api_server_key        = "replace-with-random-bearer-key"

# Optional Google Cloud integrations (enable drift_pubsub and drift_detector together):
enable_drift_pubsub   = false
enable_drift_detector = false
enable_github_minter  = false
enable_google_chat    = false
```

Apply the composition through `lifecycle.sh`:

```bash
cd terraform/examples/full-install
KUBE_AGENTS_STATE_BUCKET=auto ./lifecycle.sh apply -auto-approve
```

After the apply finishes, Terraform exposes three outputs for your Kubernetes deployment pipeline:

| Output                      | Contents                                                                                                                                                         |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `kube_agents_helm_values`   | Ordered list of rendered YAML values documents for the `kube-agents` chart, including Workload Identity service account emails and enabled integration settings. |
| `kube_agents_helm_release`  | Release name (`kube-agents`), target namespace (`kubeagents-system`), relative chart path, and chart version.                                                    |
| `cert_manager_helm_release` | Repository, chart, version, namespace, and values for `cert-manager`, or `null` when `enable_cert_manager = false`.                                              |

Inspect the rendered Helm values documents from Terraform state:

```bash
terraform output -json kube_agents_helm_values | jq -r '.[0]' > values-0.yaml
terraform output -json kube_agents_helm_values | jq -r '.[1]' > values-1.yaml
```

Do not commit `values-0.yaml` directly to Git. Terraform renders `platformAgent.credentials.create: true` in `kube_agents_helm_values[0]` and embeds plaintext secrets (`API_SERVER_KEY`, `SESSION_KV_API_KEY`, `SESSION_KV_SALT`, and `SANDBOX_SSH_PRIVATE_KEY`) under `platformAgent.credentials.data`. For Flux or Argo CD, either strip the `platformAgent.credentials.data` block and set `platformAgent.credentials.create: false` before committing, or commit the secret-free values shown under [Minimal Helm chart values](#minimal-helm-chart-values) and deliver the Secret keys through your secret manager.

### Option B: Compose `terraform/modules/` directly

If your organization maintains its own Terraform root module, call the versioned modules under [`terraform/modules/`](https://github.com/gke-labs/kube-agents/tree/main/terraform/modules) directly, pinned to a release tag ([Pinning Terraform module versions in GitOps](/kube-agents/deploy/release-versioning/#pinning-terraform-module-versions-in-gitops)):

```hcl
module "platform_agent_iam" {
  source             = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/kube-agents-iam?ref=<RELEASE_VERSION>"
  project_id         = var.project_id
  service_account_id = "kubeagents-platform-gsa"
  namespace          = "kubeagents-system"
  ksa_name           = "kubeagents-platform-agent"
}

# Include when litellm.modelProvider is vertex_ai:
module "litellm_vertex_iam" {
  source             = "git::https://github.com/gke-labs/kube-agents.git//terraform/modules/kube-agents-iam?ref=<RELEASE_VERSION>"
  project_id         = var.project_id
  service_account_id = "kubeagents-litellm-gsa"
  display_name       = "Kube-Agents LiteLLM Vertex AI Service Account"
  namespace          = "kubeagents-system"
  ksa_name           = "kubeagents-litellm"
  project_roles      = ["roles/aiplatform.user"]
}
```

Add [`terraform/modules/drift-pubsub`](https://github.com/gke-labs/kube-agents/tree/main/terraform/modules/drift-pubsub) when you enable `platformAgent.harness.driftDetector.enabled: true` in the chart, [`terraform/modules/github-minter`](https://github.com/gke-labs/kube-agents/tree/main/terraform/modules/github-minter) for the GitHub App token minter, and [`terraform/modules/chat-pubsub`](https://github.com/gke-labs/kube-agents/tree/main/terraform/modules/chat-pubsub) for Google Chat ingress. See [Security & IAM](/kube-agents/reference/security-and-iam/#gcp-iam-permission-sets) for the default read-only role list.

## External Secrets for GitOps

In a GitOps workflow, keep credentials out of Git by setting `platformAgent.credentials.create: false` and provisioning two Kubernetes Secrets in `kubeagents-system` before or alongside the Helm release. The first Secret, `platform-agent-secrets` (`platformAgent.credentials.secretName`, which must keep this default name), holds `API_SERVER_KEY`, `SESSION_KV_API_KEY`, `SESSION_KV_SALT`, `SANDBOX_SSH_PRIVATE_KEY`, `SANDBOX_SSH_PUBLIC_KEY`, and any model provider API key (omitted when `litellm.modelProvider` is `vertex_ai` on Workload Identity). The second Secret, `platform-agent-shell-authorized-keys`, holds a single `authorized_keys` key containing the public key line matching `SANDBOX_SSH_PUBLIC_KEY` so the shell sandbox pod never mounts private keys or API tokens.

When `platformAgent.credentials.create` is `false`, the chart uses Helm's `lookup` function to copy `SANDBOX_SSH_PUBLIC_KEY` from `platform-agent-secrets` into `platform-agent-shell-authorized-keys`. Offline template renderers such as Argo CD disable `lookup`, so the chart omits `platform-agent-shell-authorized-keys` unless you provision it through your secret manager. For the same reason, set `litellm.masterKey.generate: false` so offline renders do not rotate `litellm-master-key` on every sync.

See [Deploying kube-agents via Argo CD without Helm lookup](/kube-agents/deploy/gitops-argocd/#deploying-kube-agents-via-argo-cd-without-helm-lookup) for the required and optional Secret key table, `ExternalSecret` manifests, and the [sandbox SSH keypair rotation procedure](/kube-agents/deploy/gitops-argocd/#rotating-the-sandbox-ssh-keypair).

## Minimal Helm chart values

When bringing your own `cert-manager` and external Google Cloud IAM on an existing GKE cluster, enable the operator's admission webhooks (`operator.webhooks.enabled: true`) and pass your Workload Identity service account annotations. The chart leaves `operator.webhooks.enabled` set to `false` by default so a bare `helm install` without `cert-manager` does not fail on missing `Certificate` CRDs.

```yaml
operator:
  webhooks:
    enabled: true
  metrics:
    podMonitoring: true

platformAgent:
  name: platform-agent
  credentials:
    create: false
    secretName: platform-agent-secrets
  harness:
    clusterName: <CLUSTER_NAME>
    location: <REGION>
    projectId: <PROJECT_ID>
    # Enable only when the drift-pubsub topic and subscription exist in the project:
    # driftDetector:
    #   enabled: true
  security:
    serviceAccountAnnotations:
      iam.gke.io/gcp-service-account: kubeagents-platform-gsa@<PROJECT_ID>.iam.gserviceaccount.com
  podMonitoring: true
  serviceMonitor: false
  vmPodScrape: false

litellm:
  masterKey:
    generate: false
  modelProvider: vertex_ai
  vertex:
    serviceAccountAnnotations:
      iam.gke.io/gcp-service-account: kubeagents-litellm-gsa@<PROJECT_ID>.iam.gserviceaccount.com
  podMonitoring: true

hindsight:
  podMonitoring: false
```

The `podMonitoring`, `serviceMonitor`, and `vmPodScrape` flags default to `null` (auto-detect via `.Capabilities.APIVersions.Has`). Setting them explicitly guarantees deterministic output across offline `helm template` pipelines. Only set `platformAgent.harness.driftDetector.enabled: true` when you have provisioned the drift audit-log Pub/Sub subscription via `enable_drift_pubsub = true` in Option A or `terraform/modules/drift-pubsub` in Option B.

## Pipeline 2: Reconcile the chart with Flux or Argo CD

Each official release publishes the signed chart to `oci://ghcr.io/gke-labs/kube-agents/charts/kube-agents` at version `<RELEASE_VERSION>` (for example, `0.29.0`, with no `v` prefix). Pass the [Minimal Helm chart values](#minimal-helm-chart-values) above in your Flux `HelmRelease` or Argo CD `Application`.

### Flux (`OCIRepository` and `HelmRelease`)

Define an `OCIRepository` pointing at GHCR and a `HelmRelease` in `kubeagents-system` that installs and upgrades the chart with `crds: CreateReplace`:

```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: OCIRepository
metadata:
  name: kube-agents
  namespace: flux-system
spec:
  interval: 15m
  url: oci://ghcr.io/gke-labs/kube-agents/charts/kube-agents
  ref:
    tag: "<RELEASE_VERSION>"
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: kube-agents
  namespace: kubeagents-system
spec:
  interval: 15m
  timeout: 15m
  chartRef:
    kind: OCIRepository
    name: kube-agents
    namespace: flux-system
  install:
    createNamespace: true
    crds: CreateReplace
  upgrade:
    crds: CreateReplace
  values:
    operator:
      webhooks:
        enabled: true
      metrics:
        podMonitoring: true
    platformAgent:
      name: platform-agent
      credentials:
        create: false
        secretName: platform-agent-secrets
      harness:
        clusterName: <CLUSTER_NAME>
        location: <REGION>
        projectId: <PROJECT_ID>
      security:
        serviceAccountAnnotations:
          iam.gke.io/gcp-service-account: kubeagents-platform-gsa@<PROJECT_ID>.iam.gserviceaccount.com
      podMonitoring: true
      serviceMonitor: false
      vmPodScrape: false
    litellm:
      masterKey:
        generate: false
      modelProvider: vertex_ai
      vertex:
        serviceAccountAnnotations:
          iam.gke.io/gcp-service-account: kubeagents-litellm-gsa@<PROJECT_ID>.iam.gserviceaccount.com
      podMonitoring: true
    hindsight:
      podMonitoring: false
```

### Argo CD (`Application`)

In Argo CD, reference the OCI chart registry under `spec.source`, include `kubeagents.x-k8s.io/v1alpha1/Tier` in `helm.apiVersions` so the chart renders the default `read` and `unattended` `Tier` resources during offline templating, and enable `ServerSideApply=true` for CRD annotations:

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
    targetRevision: "<RELEASE_VERSION>"
    helm:
      apiVersions:
        - kubeagents.x-k8s.io/v1alpha1/Tier
      valuesObject:
        operator:
          webhooks:
            enabled: true
          metrics:
            podMonitoring: true
        platformAgent:
          name: platform-agent
          credentials:
            create: false
            secretName: platform-agent-secrets
          harness:
            clusterName: <CLUSTER_NAME>
            location: <REGION>
            projectId: <PROJECT_ID>
          security:
            serviceAccountAnnotations:
              iam.gke.io/gcp-service-account: kubeagents-platform-gsa@<PROJECT_ID>.iam.gserviceaccount.com
          podMonitoring: true
          serviceMonitor: false
          vmPodScrape: false
        litellm:
          masterKey:
            generate: false
          modelProvider: vertex_ai
          vertex:
            serviceAccountAnnotations:
              iam.gke.io/gcp-service-account: kubeagents-litellm-gsa@<PROJECT_ID>.iam.gserviceaccount.com
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

For standing up Argo CD itself on GKE with Workload Identity and Config Connector, see [GitOps with ArgoCD](/kube-agents/deploy/gitops-argocd/).

## Verify the deployment

Once Flux or Argo CD syncs the release, wait for the `PlatformAgent` resource to report `Ready`:

```bash
kubectl wait platformagent/platform-agent -n kubeagents-system --for=condition=Ready --timeout=25m
kubectl get pods -n kubeagents-system
```

Follow [Verify the A2A stack](/kube-agents/install/quickstart-gke/#verify-the-a2a-stack) to inspect the message bus and gateway conditions, then send your first query with [First prompts](/kube-agents/install/first-prompts/).
