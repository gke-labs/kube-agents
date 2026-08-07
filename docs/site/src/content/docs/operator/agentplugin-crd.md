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
| `targetProfile`   | string            | No       | Install the plugin into a named Hermes profile (for example `platform`) instead of the default one.               |
| `config`          | string            | No       | YAML configuration overrides merged into the target profile's Hermes `config.yaml`.                               |

## Architecture & How It Works

1. **Naming**:
   `metadata.name` must match `^[a-z][a-z0-9]*$` and be at most 56 characters, enforced by a CEL rule on the CRD. The name is used three ways — as the mount directory, as the entry in `plugins.enabled`, and as the module identifier Hermes imports — so hyphens, dots, underscores, and uppercase are rejected up front rather than failing later at mount or import time.
2. **OCI Image Volume Mounting**:
   Plugin assets packaged in OCI container images are mounted using Kubernetes OCI Image Volumes (`corev1.ImageVolumeSource`) at `$PLATFORM_AGENT_HOME/plugins/<plugin-name>` (e.g., `/opt/data/plugins/<plugin-name>`) via a volume named `plugin-<plugin-name>`. With `targetProfile` set, the image is mounted at `/opt/agent-plugins/<profile>/<plugin-name>` — outside the data PVC — and the entrypoint links it into `$PLATFORM_AGENT_HOME/profiles/<profile>/plugins/<plugin-name>`, where Hermes resolves a profile's plugins from. The mount and the link both belong to the gateway container: sidecars built from the same image share the data PVC but not the plugin volumes, so the entrypoint's setup runs only there.

   The indirection is not cosmetic. The kubelet creates a volume's mount point before the container entrypoint runs, so mounting into `profiles/<profile>/` would create that directory on the PVC ahead of the profile's own scaffold — and a directory is what every "is this profile built?" check reads. A fresh volume would come up with a profile that was never registered with Hermes and never received its skills, and because the directory persists, no later restart would repair it. Staging outside the PVC and linking in keeps the kubelet out of the profile tree.

3. **Plugin Auto-Registration**:
   The operator automatically appends `metadata.name` to Hermes `config.yaml` under `plugins.enabled`. Names that collide with a built-in Hermes plugin after separators are stripped (for example `sessionstore` against the built-in `session_store`) are refused, and the plugin is marked `Degraded` with `Reason: DuplicatePluginName`.
4. **Profile Targeting**:
   A Hermes plugin is only usable by the profile it is installed in — the profile's load is what runs the plugin's `register(ctx)` hook, and hooks such as `ctx.register_skill()` are what make its skills resolvable. Mounting alone is not enough: a plugin absent from that profile's `plugins.enabled` is inert. So the operator emits one config overlay per targeted profile into the same ConfigMap, as `profile-<profile>.overlay.yaml`, carrying the `plugins.enabled` entry plus the plugin's allowlisted `spec.config` subtrees. The entrypoint merges each overlay into `profiles/<profile>/config.yaml` at startup, after the image force-sync that would otherwise overwrite it. Mount and enablement are always written together — for any profile name, `cluster-<...>` included — so a plugin cannot be present but inert.

   The operator renders an overlay rather than the whole profile config on purpose — see [how config reaches each profile](/kube-agents/operator/platformagent-crd/#how-config-reaches-each-profile), which is canonical on the delivery mechanism, its ordering constraint, and its merge semantics.

   A plugin targeting a profile is **not** enabled on the default profile. The operator cannot verify the profile exists, because profiles are scaffolded at pod startup rather than by the operator; a name matching no profile yields a plugin that never loads, and the entrypoint logs a warning naming the missing profile. A `cluster-<...>` name is reported as a note instead of a warning: those profiles appear when their cluster is onboarded, and that profile links and enables the plugin as it is scaffolded.

5. **Config Subtree Allowlisting**:
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
