---
title: AgentPlugin fields
description: Every spec and status field of the AgentPlugin custom resource, generated from the operator's Go API types.
sidebar:
  order: 5
---

<!-- BEGIN GENERATED: crd-fields-agentplugin -->
<!-- Regenerate with: make docs-generate -- do not edit by hand. -->
<!-- prettier-ignore-start -->

Every field of the `AgentPlugin` resource's `spec` and `status`, generated from the operator's Go API types. The standard Kubernetes `apiVersion`, `kind` and `metadata` are not listed. A field whose type comes from the Kubernetes API (`corev1`, `metav1`, `networkingv1`, `resource.Quantity`) names that type and is not expanded; the Kubernetes API reference describes it. Paths read `parent.child`, with `[]` for each item of a list and `{}` for each value of a map. Each description is the opening paragraph of the field's documentation, plus its deprecation notice when it has one, and names other fields in their Go spelling (`MaxSessions` for `maxSessions`); `kubectl explain agentplugin.spec.<field>` (`agentplugin.status.<field>` for a row of the Status table) prints the whole text. The Constraints column lists the schema bounds on a field: allowed values, ranges, lengths and patterns. The API server also enforces CEL rules on these fields and on whole sections, which the column does not show; `kubectl get crd agentplugins.kubeagents.x-k8s.io -o yaml` lists them under `x-kubernetes-validations`, each with the message the API server returns when a value breaks it. For what the fields do together, read the [AgentPlugin CRD](/kube-agents/operator/agentplugin-crd/) guide.

## Spec

| Field | Type | Required | Default | Constraints | Description |
| ----- | ---- | -------- | ------- | ----------- | ----------- |
| `agentRef` | `string` | yes |  |  | AgentRef references the specific PlatformAgent instance name. |
| `image` | `string` | yes |  |  | Image is the OCI image reference containing the plugin. |
| `imagePullPolicy` | `corev1.PullPolicy` | no |  | one of `Always`, `Never`, `IfNotPresent` | ImagePullPolicy specifies if the image should be pulled. Defaults to IfNotPresent. |
| `targetProfile` | `string` | no |  | pattern `^[a-z0-9][a-z0-9-]*$`; max length `63` | TargetProfile installs this plugin into a named Hermes profile (for example "platform") instead of the default one. Empty means the default profile. |
| `config` | `string` | no |  |  | Config allows providing runtime overrides merged into the agent's config.yaml. When TargetProfile is set the overrides are merged into that profile's config instead of the default profile's. |
| `env` | `[]corev1.EnvVar` | no |  |  | Env specifies additional environment variables for the agent. |

## Status

| Field | Type | Required | Default | Constraints | Description |
| ----- | ---- | -------- | ------- | ----------- | ----------- |
| `phase` | `string` | no |  |  | Phase is the status phase of the plugin (e.g. "Ready", "Error"). |
| `observedGeneration` | `integer` | no |  |  | ObservedGeneration is the .metadata.generation the status was last computed from. |
| `targetAgents` | `[]string` | no |  |  | TargetAgents lists the names of PlatformAgent instances this plugin is applied to. |
| `lastUpdated` | `metav1.Time` | no |  |  | LastUpdated is the timestamp when the plugin was last processed. |
| `conditions` | `[]metav1.Condition` | no |  |  | Conditions represent the latest available observations of the plugin's state. |

<!-- prettier-ignore-end -->
<!-- END GENERATED: crd-fields-agentplugin -->
