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
  name: stockout-handler
  namespace: kubeagents-system
spec:
  agentRef: "platform-agent"
  image: "us-docker.pkg.dev/my-project/plugins/stockout-handler:v1.0.0"
  imagePullPolicy: PullIfNotPresent
  env:
    - name: SLACK_API_TOKEN
      valueFrom:
        secretKeyRef:
          name: slack-secrets
          key: api-token
  config: |
    approvals:
      auto_approve_read_only: true
```

### Key Fields

| Field             | Type              | Required | Purpose                                                                                                           |
| ----------------- | ----------------- | -------- | ----------------------------------------------------------------------------------------------------------------- |
| `agentRef`        | string            | Yes      | Target `PlatformAgent` instance name. Targeting is strictly opt-in; omitting `agentRef` will not match any agent. |
| `image`           | string            | Yes      | OCI image reference containing plugin assets (skills, prompts, tools).                                            |
| `imagePullPolicy` | string            | No       | Image pull policy for OCI image volume (`PullIfNotPresent`, `Always`, `Never`). Default `PullIfNotPresent`.       |
| `env`             | `[]corev1.EnvVar` | No       | Additional environment variables (including secret references) injected into the agent pod spec.                  |
| `config`          | string            | No       | YAML configuration overrides merged into Hermes `config.yaml`.                                                    |

## Architecture & How It Works

1. **OCI Image Volume Mounting**:
   Plugin assets packaged in OCI container images are mounted using Kubernetes OCI Image Volumes (`corev1.ImageVolumeSource`) at `$PLATFORM_AGENT_HOME/plugins/<plugin-name>` (e.g., `/opt/data/plugins/<plugin-name>`). Note that `metadata.name` defines the mount directory name and plugin identifier.
2. **Plugin Auto-Registration**:
   The operator automatically appends `metadata.name` to Hermes `config.yaml` under `plugins.enabled`.
3. **Config Subtree Allowlisting**:
   To preserve the operator's security posture, `spec.config` YAML overrides are restricted to top-level subtrees `["approvals", "platforms", "platform_toolsets"]`. Any attempt to override restricted subtrees (such as `agent.disabled_toolsets`, `leader_election`, or `logging`) is rejected and logged as an error by the operator (`manifestsLog.Error`).

## Requirements & Compatibility Gating

- **Kubernetes Version**: Native `ImageVolumeSource` support requires **Kubernetes 1.35+** (where the feature gate is enabled by default).
- **Older Cluster Guard**: On clusters running Kubernetes < 1.35 where OCI image volumes are unsupported, the operator skips OCI volume attachment to prevent Pod spec validation failures. The plugin status is updated to `Phase: Degraded` with condition `Reason: ImageVolumeUnsupported`.
- **Annotation Override**: Image volume support can be explicitly toggled via the `kubeagents.x-k8s.io/enable-image-volumes="true"|"false"` annotation on the `PlatformAgent` resource.
- **Decoupled Dependency**: The operator reconciles `PlatformAgent` workloads gracefully even if the `AgentPlugin` CRD is not installed on the cluster.
