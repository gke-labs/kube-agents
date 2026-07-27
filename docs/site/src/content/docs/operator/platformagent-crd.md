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
  deployment: { ... } # container image, pull policy, containers, volumes
  security: { ... } # service account + Workload Identity
  integration: { ... } # Google Chat, Slack, GitHub
```

`spec.deployment` and `spec.security` are inlined from the shared `AgentSpec`, so they are common to every agent type. `spec.harness` is required; `spec.integration` is optional.

## `spec.harness`

Framework-level settings passed to Hermes. `clusterName` and `location` are required.

| Field                                    | Type   | Purpose                                                                              |
| ---------------------------------------- | ------ | ------------------------------------------------------------------------------------ |
| `clusterName`                            | string | Logical cluster name (e.g. `cluster-a`). Surfaces in observability and chat replies. |
| `location`                               | string | Cloud region (e.g. `us-central1-a`).                                                 |
| `projectId`                              | string | GCP Project ID of the cluster. Optional.                                             |
| `hermes.dashboardEnabled`                | bool   | Toggle the Hermes dashboard endpoint. Default `true`.                                |
| `hermes.pluginsDebug`                    | bool   | Enable plugin-level debug logging. Default `false`.                                  |
| `hermes.agentHome`                       | string | Path to the `AGENT_HOME` directory. Default `/opt/data`.                             |
| `hermes.apiServerSecretRef.name` + `key` | string | `Secret` holding the Hermes API server key (`API_SERVER_KEY`).                       |
| `memory.memoryEnabled`                   | bool   | Toggle framework memory persistence. Default `false`.                                |
| `memory.provider`                        | string | Memory provider implementation. Default `multiuser_memory`.                          |
| `memory.userProfileEnabled`              | bool   | Toggle per-user memory profiling. Default `false`.                                   |

## `spec.deployment`

Abstracts the pod/deployment configuration. The controller synthesises a `Deployment` from these plus the workspace ConfigMaps. Available fields:

- `image` — container image repository.
- `tag` — image tag. Default `latest`.
- `imagePullPolicy` — one of `Always`, `Never`, `IfNotPresent`. Default `IfNotPresent`.
- `browserArgs` — extra command-line args for the agent's browser (e.g. `--no-sandbox`).
- `runtimeClassName` — pod runtime class (e.g. `gvisor`).
- `env` — additional container environment variables.
- `initContainers` / `sidecars` — standard init and sidecar containers.
- `extraVolumes` / `extraVolumeMounts` — custom volumes and mounts for the main container.
- `sidecarVolumes` — custom volumes for the sidecar containers.
- `podAnnotations` — annotations applied to the generated pod template.
- `scaleToZero` — when `true`, scales the deployment to 0 replicas (idle cost saving).

Default image: `ghcr.io/gke-labs/kube-agents/platform-agent:latest`. Rebuild with `make dev-rebuild-agent ARGS="platform"` for local iteration.

## `spec.security`

- `serviceAccountName` — the KSA the pod runs as. `kubeagents-platform-agent` by convention.
- `serviceAccountAnnotations` — passed through to the KSA. Typically holds `iam.gke.io/gcp-service-account` for Workload Identity binding.

The Workload Identity target GSA (`kubeagents-platform-gsa@<project>.iam.gserviceaccount.com`) is created and bound by `provision_04_gcp_iam.sh` with one of these permission sets:

- `read-only`
- `gke-admin` (default)
- `custom` (roles supplied via `PLATFORM_AGENT_CUSTOM_ROLES`)

## `spec.integration`

Enables external integrations. Only the enabled ones need to be present.

- **`googleChat`** — `enabled` (default `false`), `projectId`, `topicName`, `subscriptionName`, `allowedUsers`, `homeChannel`, and `mode` (`default` or `debug`, default `default`). When `enabled`, `projectId`, `topicName`, and `subscriptionName` are required (enforced by a CEL validation rule). Populated by `provision_05_gcp_gchat.sh`.
- **`slack`** — `enabled` (default `false`), `botTokenSecretRef` and `appTokenSecretRef` (Secret refs, required when enabled), `allowedUsers`, `homeChannel`, and `homeChannelName`. Populated by `provision_06_slack.sh` when `SLACK_ENABLED=true`.
- **`github`** — `gitRepo`, the target GitOps repository URL for the agent environment. Populated by `provision_10_deploy_github_minter.sh`.

See [`k8s-operator/api/v1alpha1/platformagent_types.go`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/api/v1alpha1/platformagent_types.go) for the exact struct definitions.

## `status`

The operator writes observed state to the `status` subresource:

| Field                            | Type   | Purpose                                                       |
| -------------------------------- | ------ | ------------------------------------------------------------- |
| `phase`                          | string | Overall state (`Pending`, `Provisioning`, `Ready`, `Failed`). |
| `address`                        | string | Fully qualified domain name (FQDN) of the agent service.      |
| `lastReconcileTime`              | time   | Timestamp of the last status update.                          |
| `conditions`                     | list   | Standard `metav1.Condition` observations, keyed by `type`.    |
| `deploymentStatus.name`          | string | Name of the underlying Deployment.                            |
| `deploymentStatus.readyReplicas` | int32  | Number of fully ready replicas.                               |
| `serviceStatus.endpoint`         | string | Primary URL/IP (with protocol and port) to reach the agent.   |
| `storageStatus.bound`            | bool   | Whether the primary PVC has been provisioned.                 |

## Reconcile behavior

- On create/update, the controller ensures the Deployment, Service, ServiceAccount, and ConfigMaps match the spec.
- On delete, it garbage-collects owned resources.
- The admission webhook (behind cert-manager) validates the spec before it's persisted; it enforces at most one `PlatformAgent` per project.
- `provision_08_deploy_platform_agent.sh` renders and applies the CR; you can also edit it directly with `kubectl edit`.

## Where to go next

- [Development](/kube-agents/operator/development/) — build and test the controller locally.
- [Provisioning scripts](/kube-agents/operator/provisioning-scripts/) — how the CR gets applied in a fresh install.
