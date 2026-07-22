---
title: PlatformAgent CRD
description: The single custom resource the operator reconciles.
sidebar:
  order: 1
---

The `PlatformAgent` resource declares everything the operator needs to run one Platform Agent instance: which Hermes image, which service account, which chat integrations, and which framework-level toggles.

- **API group / version**: `kubeagents.x-k8s.io/v1alpha1`
- **Kind**: `PlatformAgent`
- **Source**: [`k8s-operator/api/v1alpha1/platformagent_types.go`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/api/v1alpha1/platformagent_types.go)
- **Sample**: [`k8s-operator/examples/platformagent.yaml`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/examples/platformagent.yaml)

## Top-level shape

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: PlatformAgent
metadata:
  name: platformagent
  namespace: kubeagents-system
spec:
  harness: { ... } # execution environment + framework
  deployment: { ... } # container image, pull policy, resources
  security: { ... } # service account + Workload Identity
  integration: { ... } # Google Chat, Slack, GitHub
```

## `spec.harness`

Framework-level settings passed to Hermes.

| Field                                    | Type   | Purpose                                                                              |
| ---------------------------------------- | ------ | ------------------------------------------------------------------------------------ |
| `clusterName`                            | string | Logical cluster name (e.g. `cluster-a`). Surfaces in observability and chat replies. |
| `location`                               | string | Cloud region (e.g. `us-central1-a`).                                                 |
| `hermes.dashboardEnabled`                | bool   | Toggle the Hermes dashboard endpoint. Default `true`.                                |
| `hermes.pluginsDebug`                    | bool   | Enable plugin-level debug logging. Default `false`.                                  |
| `hermes.apiServerSecretRef.name` + `key` | string | `Secret` holding the Hermes API server key.                                          |

## `spec.deployment`

Standard container spec: `image`, `imagePullPolicy`, `resources`, node selectors, tolerations. The controller synthesises a `Deployment` from these plus the workspace ConfigMaps.

Default image: `ghcr.io/gke-labs/kube-agents/platform-agent`. Rebuild with `make dev-rebuild-agent ARGS="platform"` for local iteration.

## `spec.security`

- `serviceAccountName` — the KSA the pod runs as. `kubeagents-platform-agent` by convention.
- `serviceAccountAnnotations` — passed through to the KSA. Typically holds `iam.gke.io/gcp-service-account` for Workload Identity binding.

The Workload Identity target GSA (`kubeagents-platform-gsa@<project>.iam.gserviceaccount.com`) is created and bound by `provision_04_gcp_iam.sh` with one of these permission sets:

- `read-only` (default)
- `gke-admin`
- `custom`

## `spec.integration`

Enables external integrations. Only the enabled ones need to be present.

- **`googleChat`** — Pub/Sub subscription name, project ID, allowed users. Populated by `provision_05_gcp_gchat.sh`.
- **`slack`** — token Secret refs, home channel, allowed users. Populated by `provision_06_slack.sh` when `SLACK_ENABLED=true`.
- **`github`** — Minty endpoint, GitOps repo URL. Populated by `provision_10_deploy_github_minter.sh`.

See [`k8s-operator/api/v1alpha1/platformagent_types.go`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/api/v1alpha1/platformagent_types.go) for the exact struct definitions.

## Reconcile behavior

- On create/update, the controller ensures the Deployment, Service, ServiceAccount, and ConfigMaps match the spec.
- On delete, it garbage-collects owned resources.
- The admission webhook (behind cert-manager) validates the spec before it's persisted.
- `provision_08_deploy_platform_agent.sh` renders and applies the CR; you can also edit it directly with `kubectl edit`.

## Where to go next

- [Development](/kube-agents/operator/development/) — build and test the controller locally.
- [Provisioning scripts](/kube-agents/operator/provisioning-scripts/) — how the CR gets applied in a fresh install.
