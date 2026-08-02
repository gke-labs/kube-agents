---
title: AgentPlugin CRD
description: Custom resource for injecting OCI packaged plugins, skills, environment variables, and allowed configuration overrides into PlatformAgent workloads.
sidebar:
  order: 2
---

The `AgentPlugin` custom resource declares external plugin extensions (skills, tools, prompt overrides, secret environment variables, and configuration) for `PlatformAgent` workloads.

- **API group / version**: `kubeagents.x-k8s.io/v1alpha1`
- **Kind**: `AgentPlugin`
- **Short Name**: `ap`
- **Source**: [`k8s-operator/api/v1alpha1/agentplugin_types.go`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/api/v1alpha1/agentplugin_types.go)
- **Sample**: [`k8s-operator/config/crd/bases/kubeagents.x-k8s.io_agentplugins.yaml`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/config/crd/bases/kubeagents.x-k8s.io_agentplugins.yaml)

## Specification

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: AgentPlugin
metadata:
  # Must match ^[a-z][a-z0-9]*$ (max 56 chars): the name is both the mount
  # directory and the module identifier Hermes imports.
  name: stockouthandler
  namespace: kubeagents-system
spec:
  agentRef: "platform-agent"
  image: "us-docker.pkg.dev/my-project/plugins/stockouthandler:v1.0.0"
  imagePullPolicy: IfNotPresent
  env:
    - name: SLACK_API_TOKEN
      valueFrom:
        secretKeyRef:
          name: slack-secrets
          key: api-token
  config: |
    platform_toolsets:
      google_chat:
        - stockout
```

### Key Fields

| Field             | Type              | Required | Purpose                                                                                                           |
| ----------------- | ----------------- | -------- | ----------------------------------------------------------------------------------------------------------------- |
| `agentRef`        | string            | Yes      | Target `PlatformAgent` instance name. Targeting is strictly opt-in; omitting `agentRef` will not match any agent. |
| `image`           | string            | Yes      | OCI image reference containing plugin assets (skills, prompts, tools).                                            |
| `imagePullPolicy` | string            | No       | Image pull policy for the OCI image volume. One of `Always`, `Never`, `IfNotPresent`. Default `IfNotPresent`.     |
| `env`             | `[]corev1.EnvVar` | No       | Additional environment variables (including secret references) injected into the agent pod spec.                  |
| `config`          | string            | No       | YAML configuration overrides merged into Hermes `config.yaml`.                                                    |

## Architecture & How It Works

1. **Naming**:
   `metadata.name` must match `^[a-z][a-z0-9]*$` and be at most 56 characters, enforced by a CEL rule on the CRD. The name is used three ways — as the mount directory, as the entry in `plugins.enabled`, and as the module identifier Hermes imports — so hyphens, dots, underscores, and uppercase are rejected up front rather than failing later at mount or import time.
2. **OCI Image Volume Mounting**:
   Plugin assets packaged in OCI container images are mounted using Kubernetes OCI Image Volumes (`corev1.ImageVolumeSource`) at `$PLATFORM_AGENT_HOME/plugins/<plugin-name>` (e.g., `/opt/data/plugins/<plugin-name>`) via a volume named `plugin-<plugin-name>`.
3. **Plugin Auto-Registration**:
   The operator automatically appends `metadata.name` to Hermes `config.yaml` under `plugins.enabled`. Names that collide with a built-in Hermes plugin after separators are stripped (for example `sessionstore` against the built-in `session_store`) are refused, and the plugin is marked `Degraded` with `Reason: DuplicatePluginName`.
4. **Config Subtree Allowlisting**:
   `spec.config` overrides are restricted to the top-level subtrees `approvals`, `platforms`, and `platform_toolsets`. Any other key (such as `agent`, `leader_election`, or `logging`) is dropped and logged as an error by the operator. Within an allowlisted subtree, list values are unioned with the operator's own entries rather than replacing them. This is a scoping mechanism, not a sandbox — see the [AgentPlugin trust boundary](/kube-agents/reference/security-and-iam/#change-control--safety) for what it does and does not prevent.

## Requirements & Compatibility Gating

- **Kubernetes Version**: Native `ImageVolumeSource` support requires **Kubernetes 1.35+** (where the feature gate is enabled by default; it is beta but off by default in 1.33 and 1.34).
- **Older Cluster Guard**: On clusters running Kubernetes < 1.35 where OCI image volumes are unsupported, the operator skips OCI volume attachment to prevent Pod spec validation failures. The plugin status is updated to `Phase: Degraded` with condition `Reason: ImageVolumeUnsupported`.
- **Fail-closed capability probe**: The operator resolves the cluster's ImageVolume capability once, from the API server version. If that probe fails or the version cannot be parsed, image volumes are treated as **unsupported** — attaching one the cluster cannot honour would make the API server reject the entire agent Deployment, which is a worse failure than leaving plugins unloaded.
- **Annotation Override**: Image volume support can be explicitly toggled via the `kubeagents.x-k8s.io/enable-image-volumes="true"|"false"` annotation on the `PlatformAgent` resource. The annotation wins over the version probe in both directions, so a 1.33 or 1.34 cluster that has the `ImageVolume` feature gate enabled manually can opt back in.
- **Decoupled Dependency**: The operator reconciles `PlatformAgent` workloads gracefully even if the `AgentPlugin` CRD is not installed on the cluster.
- **Restart the operator after installing the CRD**: the `AgentPlugin` watch is registered at operator startup only. If the CRD is installed into a running cluster afterwards, restart the controller manager so it picks the watch up.
- **Plugin changes restart the agent**: adding, changing, or removing an `AgentPlugin` alters the agent's `config.yaml` and pod spec, so the agent pod is rolled. Hermes loads plugins at startup and does not hot-reload them.
- **A bad plugin image takes the agent down**: the OCI volume lives in the agent's pod spec, so an image that cannot be pulled keeps the whole agent pod from starting — not just that plugin from loading. Prefer immutable digests over mutable tags, and check plugin status after any image change.

## Status

`status.phase` is `Ready` or `Degraded`, with the detail on the `Ready` condition:

| `Reason`                 | Meaning                                                                                                     |
| ------------------------ | ----------------------------------------------------------------------------------------------------------- |
| `Applied`                | Mounted and registered. Any config keys dropped by the allowlist are named in the message.                  |
| `AgentNotFound`          | `spec.agentRef` names no `PlatformAgent` in this namespace — usually a typo. The plugin is applied nowhere. |
| `InvalidPluginName`      | `metadata.name` breaks the naming rule (only reachable for objects created before the rule existed).        |
| `DuplicatePluginName`    | The name collides with a built-in Hermes plugin, or with another plugin, after separators are stripped.     |
| `ImageVolumeUnsupported` | The cluster cannot mount OCI image volumes, so the volume was omitted.                                      |
| `ImagePullFailed`        | `spec.image` could not be pulled. The agent pod is blocked from starting until this is fixed.               |

`status.observedGeneration` records the `metadata.generation` the status was computed
from, so a stale condition is distinguishable from a current one.
