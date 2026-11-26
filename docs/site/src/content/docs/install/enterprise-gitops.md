---
title: Enterprise Helm & GitOps
description: Deploy the kube-agents operator and Helm chart through Flux or Argo CD with separate Google Cloud IAM and an existing cert-manager.
---

Enterprise GKE teams often provision Google Cloud IAM in a dedicated Terraform pipeline and deploy Kubernetes workloads through Flux or Argo CD. This guide deploys the official [`kube-agents` Helm chart](https://github.com/gke-labs/kube-agents/tree/main/charts/kube-agents) and operator onto an existing GKE cluster without running `install.sh`.

This path installs the full operator, inference gateway, A2A stack, and `PlatformAgent` custom resource from the published OCI chart. If you instead want to copy the raw `agents/platform/` workspace files into your own Hermes runtime without the operator or chart, see [Manual install](/kube-agents/install/manual/).

## Cluster and project prerequisites

Before deploying the chart, confirm that your target GKE cluster meets the [Cluster requirements](/kube-agents/install/prerequisites/#cluster-requirements):

- **GKE 1.29 or newer** with Workload Identity (`<PROJECT_ID>.svc.id.goog`) enabled on the cluster and `GKE_METADATA` enabled on every node pool.
- **NetworkPolicy enforcement** enabled via GKE Dataplane V2 or the legacy Calico addon.
- **[cert-manager](/kube-agents/install/prerequisites/#cert-manager-on-the-target-cluster) v1.13.0+** installed on the cluster so the operator's admission webhooks receive TLS serving certificates.
- **A `gvisor` RuntimeClass** available on the cluster (included on GKE Autopilot, or via a sandboxed node pool on GKE Standard). To run on the standard container runtime instead, set `platformAgent.deployment.availability.runtimeClassName: ""` in your Helm values.
- **Release namespace `kubeagents-system`.** Keep the chart in `kubeagents-system`: the agent's default inference gateway endpoint is fixed to `http://inference-gateway.kubeagents-system.svc.cluster.local/v1`.

## Pipeline 1: Google Cloud IAM and infrastructure (Terraform)

You can provision the Google Cloud service accounts, IAM bindings, Pub/Sub topics, and Cloud KMS keys in two ways. Either run [`terraform/examples/full-install`](https://github.com/gke-labs/kube-agents/tree/main/terraform/examples/full-install) in infrastructure-only mode, or compose the individual modules under [`terraform/modules/`](https://github.com/gke-labs/kube-agents/tree/main/terraform/modules) in your own Terraform root.

### Option A: `full-install` with `install_helm_releases = false`

Setting `install_helm_releases = false` tells [`terraform/examples/full-install`](https://github.com/gke-labs/kube-agents/tree/main/terraform/examples/full-install) to provision only the Google Cloud resources and skip both `helm_release` resources. Terraform never connects to the GKE control plane in this mode, so your cloud pipeline needs no network path to a private cluster endpoint.

Pair `install_helm_releases = false` with `create_cluster = false` and `enable_cert_manager = false` in `terraform.tfvars`:

```hcl
project_id            = "my-gcp-project"
cluster_name          = "my-existing-cluster"
location              = "us-central1"
create_cluster        = false
enable_cert_manager   = false
install_helm_releases = false

# Model provider: vertex_ai uses Workload Identity with no API key.
model_provider        = "vertex_ai"
api_server_key        = "replace-with-random-bearer-key"

# Optional Google Cloud integrations:
enable_drift_pubsub   = true
enable_drift_detector = true
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

Inspect or export the rendered Helm values documents from Terraform state:

```bash
terraform output -json kube_agents_helm_values | jq -r '.[0]' > values-0.yaml
terraform output -json kube_agents_helm_values | jq -r '.[1]' > values-1.yaml
```

`kube_agents_helm_values` is marked sensitive because it includes `platformAgent.credentials.data` when Terraform generates the Secret payload. When deploying through Flux or Argo CD, set `platformAgent.credentials.create: false` in your GitOps values and deliver the Secret keys through your secret manager instead.

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

Add [`terraform/modules/drift-pubsub`](https://github.com/gke-labs/kube-agents/tree/main/terraform/modules/drift-pubsub) for out-of-band GKE audit-log drift detection, [`terraform/modules/github-minter`](https://github.com/gke-labs/kube-agents/tree/main/terraform/modules/github-minter) for the GitHub App token minter, and [`terraform/modules/chat-pubsub`](https://github.com/gke-labs/kube-agents/tree/main/terraform/modules/chat-pubsub) for Google Chat ingress. See [Security & IAM](/kube-agents/reference/security-and-iam/#gcp-iam-permission-sets) for the default read-only role list.

## External Secrets for GitOps

In a GitOps workflow, keep credentials out of Git by setting `platformAgent.credentials.create: false` and provisioning two Kubernetes Secrets in `kubeagents-system` before or alongside the Helm release:

1. **`platform-agent-secrets`** (`platformAgent.credentials.secretName`, must keep this default name) holding:
   - `API_SERVER_KEY`: random bearer token for the pod-local Platform Agent API proxy.
   - `SESSION_KV_API_KEY`: random bearer token for the pod-local Session KV server (required by `k8s-event-watcher`).
   - `SESSION_KV_SALT`: stable random HMAC salt for pseudonymising chat identities across upgrades.
   - `SANDBOX_SSH_PRIVATE_KEY` and `SANDBOX_SSH_PUBLIC_KEY`: an OpenSSH `ed25519` keypair used by the gateway pod to dial the shell sandbox pod.
   - Your model provider API key (`GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `OPENAI_COMPATIBLE_API_KEY`), omitted when `litellm.modelProvider` is `vertex_ai` on Workload Identity.
2. **`platform-agent-shell-authorized-keys`** holding a single key, `authorized_keys`, set to the public key line matching `SANDBOX_SSH_PUBLIC_KEY`. The shell sandbox mounts only this Secret so private keys and API tokens never enter the sandbox pod.

When `platformAgent.credentials.create` is `false`, the chart template uses Helm's `lookup` function to copy `SANDBOX_SSH_PUBLIC_KEY` from `platform-agent-secrets` into `platform-agent-shell-authorized-keys`. Offline template renderers such as Argo CD disable `lookup`, so the chart omits `platform-agent-shell-authorized-keys` unless you create it via your secret manager. For the same reason, set `litellm.masterKey.generate: false` so offline renders do not rotate `litellm-master-key` on every sync.

See [Deploying kube-agents via Argo CD without Helm lookup](/kube-agents/deploy/gitops-argocd/#deploying-kube-agents-via-argo-cd-without-helm-lookup) for the complete Secret key table, `ExternalSecret` manifests, and the [sandbox SSH keypair rotation procedure](/kube-agents/deploy/gitops-argocd/#rotating-the-sandbox-ssh-keypair).

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
    driftDetector:
      enabled: true
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

The `podMonitoring`, `serviceMonitor`, and `vmPodScrape` flags default to `null` (auto-detect via `.Capabilities.APIVersions.Has`). Setting them explicitly guarantees deterministic output across offline `helm template` pipelines.

## Pipeline 2: Reconcile the chart with Flux or Argo CD

Each official release publishes the signed chart to `oci://ghcr.io/gke-labs/kube-agents/charts/kube-agents` at version `<RELEASE_VERSION>` (for example, `0.29.0`, with no `v` prefix).

### Flux (`OCIRepository` and `HelmRelease`)

Define an `OCIRepository` pointing at GHCR and a `HelmRelease` that installs and upgrades the chart with `crds: CreateReplace`:

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
