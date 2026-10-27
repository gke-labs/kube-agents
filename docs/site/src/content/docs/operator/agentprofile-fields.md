---
title: AgentProfile fields
description: Every spec and status field of the AgentProfile custom resource, generated from the operator's Go API types.
sidebar:
  order: 6
---

<!-- BEGIN GENERATED: crd-fields-agentprofile -->
<!-- Regenerate with: make docs-generate -- do not edit by hand. -->
<!-- prettier-ignore-start -->

Every field of the `AgentProfile` resource's `spec` and `status`, generated from the operator's Go API types. The standard Kubernetes `apiVersion`, `kind` and `metadata` are not listed. A field whose type comes from the Kubernetes API (`corev1`, `metav1`, `networkingv1`, `resource.Quantity`) names that type and is not expanded; the Kubernetes API reference describes it. Paths read `parent.child`, with `[]` for each item of a list and `{}` for each value of a map. Each description is the opening paragraph of the field's documentation, plus its deprecation notice when it has one, and names other fields in their Go spelling (`MaxSessions` for `maxSessions`); `kubectl explain agentprofile.spec.<field>` (`agentprofile.status.<field>` for a row of the Status table) prints the whole text. The Constraints column lists the schema bounds on a field: allowed values, ranges, lengths and patterns. The API server also enforces CEL rules on these fields and on whole sections, which the column does not show; `kubectl get crd agentprofiles.kubeagents.x-k8s.io -o yaml` lists them under `x-kubernetes-validations`, each with the message the API server returns when a value breaks it. For what the fields do together, read the [AgentProfile CRD](/kube-agents/operator/agentprofile-crd/) guide.

## Spec

| Field | Type | Required | Default | Constraints | Description |
| ----- | ---- | -------- | ------- | ----------- | ----------- |
| `description` | `string` | yes |  | min length `1`; max length `4096` | Description is the routing blurb, rendered into the A2A agent card. |
| `persona` | `object` | yes |  |  | Persona is the OCI artifact holding the persona (persona.yaml, instructions.md, skills/, mcp.yaml) under its /files directory, mounted read-only at /persona. |
| `persona.image` | `string` | yes |  | min length `1` | Image is the OCI reference of the persona artifact. |
| `harness` | `object` | yes |  |  | Harness is the worker image and its model routing. |
| `harness.image` | `string` | yes |  | min length `1` | Image is the worker image: a harness speaking the headless CLI contract, wrapped by the bus adapter. |
| `harness.model` | `string` | no |  |  | Model is the model route, via LiteLLM. |
| `harness.maxTurns` | `integer` | no |  | min `0` | MaxTurns is the turn budget. |
| `tools` | `[]object` | no |  | max items `16` | Tools are OCI images of command-line tools for the profile's pods. Each is mounted read-only at /opt/tools/&lt;name&gt; and its bin/ directory is put on PATH, in list order, so one tools image serves every harness. A tools image holds no credentials: a tool that needs them (kubectl or gcloud against the fleet, a forge CLI) gets them from the broker, per task. |
| `tools[].name` | `string` | yes |  | min length `1`; max length `24`; pattern `^[a-z0-9]([-a-z0-9]{0,22}[a-z0-9])?$` | Name is the tool's directory under /opt/tools: a DNS-1123 label unique in the list, at most 24 characters so the staging init container's name ("stage-tool-&lt;name&gt;") stays within the 35 GKE Autopilot allows. |
| `tools[].image` | `string` | yes |  | min length `1` | Image is the OCI reference of the tools image. Its payload is the image's /files directory, mounted at /opt/tools/&lt;name&gt;; binaries go in /files/bin. |
| `tools[].imagePullPolicy` | `corev1.PullPolicy` | no |  | one of `Always`, `IfNotPresent`, `Never` | ImagePullPolicy is the pull policy for the image. Absent means IfNotPresent. |
| `bus` | `object` | no |  |  | Bus holds topic grants beyond the task subjects every executor gets. Absent or empty grants no topic at all. |
| `bus.publishTopics` | `[]string` | no |  | max items `32`; items pattern `^(shared\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?\|agent\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)$`; items max length `133` | PublishTopics are the topics the profile's pods may write. |
| `bus.subscribeTopics` | `[]string` | no |  | max items `32`; items pattern `^(shared\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?\|agent\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)$`; items max length `133` | SubscribeTopics are the topics the profile's pods may read. |
| `identity` | `object` | no |  |  | Identity names the ServiceAccount the profile's pods run as. Absent, the operator creates one with zero RBAC bindings: the token exists to authenticate to the bus, not to talk to the API server. |
| `identity.serviceAccountName` | `string` | no |  | max length `253`; pattern `^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$` | ServiceAccountName is an existing ServiceAccount in the profile's namespace. Naming one hands this profile's pods whatever RBAC it holds; absent, the operator creates a ServiceAccount with none. |
| `clusterRef` | `object` | no |  |  | ClusterRef is set on cluster agents only: the cluster this profile is scoped to, as structured data rather than a parsed name. |
| `clusterRef.projectId` | `string` | yes |  | min length `1` |  |
| `clusterRef.cluster` | `string` | yes |  | min length `1` |  |
| `clusterRef.location` | `string` | yes |  | min length `1` |  |
| `lifecycle` | `object` | yes |  |  | Lifecycle bounds one task's wall clock and how long the finished Job lingers. |
| `lifecycle.activeDeadlineSeconds` | `integer` | yes |  | min `1` | ActiveDeadlineSeconds is the hard ceiling on one task's wall clock, enforced by Kubernetes from Job spawn. |
| `lifecycle.ttlSecondsAfterFinished` | `integer` | yes |  | min `0` | TTLSecondsAfterFinished is how long a finished Job lingers for inspection before garbage collection. |
| `queueTimeoutSeconds` | `integer` | no |  | min `0` | QueueTimeoutSeconds is how stale a queued submission may be, by its JetStream server ingest time, before the dispatcher refuses to run it. Absent or zero means DefaultAgentProfileQueueTimeoutSeconds. |
| `concurrency` | `integer` | yes |  | min `1` | Concurrency is the most pods this profile runs at once. |
| `allowedTiers` | `[]string` | no |  | min items `1`; max items `2`; items one of `read`, `unattended` | AllowedTiers are the tiers this profile may run at. A task runs at its requester's tier, and the dispatcher refuses one whose tier is not listed here; a pinned production profile might allow only read. Tier is authority, so it lives on the CR, never in the persona. Absent means read alone (AllowedTierList). |
| `resources` | `object` | yes |  |  | Resources is the pod resource class. |
| `resources.requests` | `object` | yes |  |  | AgentProfileResourceList is a cpu and memory pair. |
| `resources.requests.cpu` | `resource.Quantity` | yes |  |  |  |
| `resources.requests.memory` | `resource.Quantity` | yes |  |  |  |
| `resources.limits` | `object` | yes |  |  | AgentProfileResourceList is a cpu and memory pair. |
| `resources.limits.cpu` | `resource.Quantity` | yes |  |  |  |
| `resources.limits.memory` | `resource.Quantity` | yes |  |  |  |

## Status

| Field | Type | Required | Default | Constraints | Description |
| ----- | ---- | -------- | ------- | ----------- | ----------- |
| `observedGeneration` | `integer` | no |  |  | ObservedGeneration is the .metadata.generation the status was last computed from. |
| `agentRef` | `string` | no |  |  | AgentRef is the PlatformAgent this profile is bound to: the one in its namespace. |
| `serviceAccountName` | `string` | no |  |  | ServiceAccountName is the ServiceAccount the profile's pods run as, whether named in spec.identity or created by the operator. |
| `conditions` | `[]metav1.Condition` | no |  |  | Conditions represent the latest available observations of the profile. |

<!-- prettier-ignore-end -->
<!-- END GENERATED: crd-fields-agentprofile -->
