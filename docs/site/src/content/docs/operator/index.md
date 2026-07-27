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

A single custom resource today:

- **`PlatformAgent`** in the `kubeagents.x-k8s.io/v1alpha1` API group.

The controller reconciles a `PlatformAgent` into:

- A `Deployment` (named `<name>-gateway`) for the Platform Agent, running the Hermes runtime with a Fluent Bit log-forwarding sidecar.
- A `Service` fronting the Deployment (API port `8642`, plus dashboard port `9119` when the dashboard is enabled).
- A `ServiceAccount` (annotated for Workload Identity) plus RBAC — a viewer `ClusterRoleBinding` and an "explorer" `ClusterRole` with its own `ClusterRoleBinding`.
- `PersistentVolumeClaim`s for the agent's data and system metadata.
- `ConfigMap`s mounted into `/opt/data/` inside the pod: the agent `config.yaml`, a `SETTINGS.md` (GKE scope / GitOps repo), and a Fluent Bit config for the logging sidecar.
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
    hermes:
      dashboardEnabled: true
      pluginsDebug: false
      apiServerSecretRef:
        name: platformagent-secrets
        key: api-key
  deployment:
    image: ghcr.io/gke-labs/kube-agents/platform-agent
    imagePullPolicy: IfNotPresent
  security:
    serviceAccountName: kubeagents-platform-agent
    serviceAccountAnnotations:
      iam.gke.io/gcp-service-account: kubeagents-platform-gsa@<project>.iam.gserviceaccount.com
  integration:
    googleChat:
      # subscription config...
```

Full walkthrough: [PlatformAgent CRD](/kube-agents/operator/platformagent-crd/).

## Related resources

- [Development](/kube-agents/operator/development/) — build, test, and run the operator locally.
- [Provisioning scripts](/kube-agents/operator/provisioning-scripts/) — the `provision_*.sh` sub-scripts.
