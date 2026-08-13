---
title: Operator overview
description: The Kubebuilder-based Go controller that reconciles PlatformAgent custom resources.
sidebar:
  order: 0
---

The `k8s-operator` is a Kubernetes controller that turns a `PlatformAgent` custom resource into a running Platform Agent Deployment plus everything it needs — Service, ServiceAccount, RBAC, PersistentVolumeClaims, and ConfigMaps for the agent config and logging. It also runs mutating (defaulting) and validating admission webhooks for the `PlatformAgent` type.

Source: [`k8s-operator/`](https://github.com/gke-labs/kube-agents/tree/main/k8s-operator). Full README: [`k8s-operator/README.md`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/README.md).

## Layout

```text
k8s-operator/
├── api/v1alpha1/           # PlatformAgent type definitions (Kubebuilder)
├── cmd/                    # manager entrypoint
├── config/                 # Kustomize base for the operator + integrations
├── internal/               # controller reconciler + admission webhook logic
├── examples/               # sample PlatformAgent CR
├── scripts/                # provision + teardown scripts
├── testing/staging_workloads/  # multi-cluster staging PoC
├── Dockerfile              # controller manager image
└── Makefile                # generate, build, test, deploy, gcp-provision
```

## What the operator manages

Custom resources in the `kubeagents.x-k8s.io/v1alpha1` API group:

- **`PlatformAgent`** — declares a Platform Agent instance, container image, service account, chat integrations, and harness toggles.
- **`AgentPlugin`** — declares OCI plugin extensions, secret environment variables, and allowed configuration overrides targeted to a `PlatformAgent`.

The controller reconciles a `PlatformAgent` into:

- A `Deployment` (named `<name>-gateway`) for the Platform Agent, running the Hermes runtime with a Fluent Bit log-forwarding sidecar.
- A `Service` fronting the Deployment (API port `8642`, plus dashboard port `9119` when the dashboard is enabled).
- A `ServiceAccount` (annotated for Workload Identity) plus RBAC — a viewer `ClusterRoleBinding` and an "explorer" `ClusterRole` with its own `ClusterRoleBinding`.
- `PersistentVolumeClaim`s for the agent's data and system metadata.
- `ConfigMap`s for the pod: config overlays merged into each Hermes profile's `config.yaml` at startup (including the whole rendered config for the default, Chat Agent, profile — see [how config reaches each profile](/kube-agents/operator/platformagent-crd/#how-config-reaches-each-profile)), a `SETTINGS.md` (GKE scope / GitOps repo) mounted into `/opt/data/`, and a Fluent Bit config for the logging sidecar. Each profile's base config is baked into the image and scaffolded at startup.
- Optional integrations wired through the CR `spec.integration` block: Google Chat (Pub/Sub topic/subscription), Slack (bot/app token secret refs), and GitHub (GitOps repo, with the GitHub Token Minter endpoint injected as an env var).

## Custom resource shape

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: PlatformAgent
metadata:
  name: platformagent
  namespace: kubeagents-system
spec:
  harness:
    clusterName: cluster-a
    location: us-central1-a
    projectId: example-project
    hermes:
      dashboardEnabled: true
      pluginsDebug: false
      apiServerSecretRef:
        name: platformagent-secrets
        key: api-key
  deployment:
    # Image is optional and omitted here on purpose. Omit it to use the
    # operator's default image (its PLATFORM_AGENT_IMAGE env var for
    # private-registry installs, else the public ghcr.io image; see the Docker
    # images page). Set it only to pin an image/registry for this agent:
    #   image: registry.example.com/kube-agents/platform-agent
    imagePullPolicy: IfNotPresent
  security:
    serviceAccountName: kubeagents-platform-agent
    serviceAccountAnnotations:
      iam.gke.io/gcp-service-account: kubeagents-platform-gsa@<project>.iam.gserviceaccount.com
  integration:
    googleChat:
      # subscription config...
```

`harness.clusterName`, `harness.location`, and `harness.projectId` are all required. The credential
proxy only bootstraps a kubectl context when it has the complete triple; leave any one out and every
`kubectl` call the agent makes resolves to `localhost:8080` instead of a cluster.

Full walkthroughs: [PlatformAgent CRD](/kube-agents/operator/platformagent-crd/) and [AgentPlugin CRD](/kube-agents/operator/agentplugin-crd/).

## Related resources

- [PlatformAgent CRD](/kube-agents/operator/platformagent-crd/) — reference for `PlatformAgent` custom resource.
- [AgentPlugin CRD](/kube-agents/operator/agentplugin-crd/) — reference for `AgentPlugin` custom resource.
- [Development](/kube-agents/operator/development/) — build, test, and run the operator locally.
- [Provisioning scripts](/kube-agents/operator/provisioning-scripts/) — the `provision_*.sh` sub-scripts.
