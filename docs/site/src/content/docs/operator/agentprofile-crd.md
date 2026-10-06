---
title: AgentProfile CRD
description: Custom resource describing one kind of A2A agent pod, and what the operator renders from it under mode next.
sidebar:
  order: 3
---

The `AgentProfile` custom resource describes one kind of agent pod on the A2A bus: its persona and worker images, its topic grants, the ServiceAccount it runs as, and how long one task may run. It is part of the `spec.mode: next` stack, which is an unsupported dev toggle. On a PlatformAgent running `today` (the default) an `AgentProfile` renders nothing.

An `AgentProfile` is not a Hermes profile. The platform agent's own profiles (the platform and cluster templates it scaffolds under `$HERMES_HOME/profiles`) are unrelated and unchanged.

- **API group / version**: `kubeagents.x-k8s.io/v1alpha1`
- **Kind**: `AgentProfile`
- **Short Name**: `apr`
- **Source**: [`k8s-operator/api/v1alpha1/agentprofile_types.go`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/api/v1alpha1/agentprofile_types.go)
- **Sample**: [`k8s-operator/examples/agentprofile-chat.yaml`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/examples/agentprofile-chat.yaml)

## Specification

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: AgentProfile
metadata:
  # A dot-free DNS-1123 label: it is the addressee on the bus. `platform` is reserved.
  name: auditor
  namespace: kubeagents-system
spec:
  description: Reads cluster state and reports findings. Never mutates.
  persona:
    image: registry.example.com/kube-agents/persona-auditor:1.0
  harness:
    image: registry.example.com/kube-agents/agent-worker:1.0
    model: model-default
    maxTurns: 50
  bus:
    publishTopics:
      - agent.auditor.findings
    subscribeTopics:
      - shared.blueprint
  identity: {} # absent: the operator creates a ServiceAccount with no RBAC
  lifecycle:
    activeDeadlineSeconds: 1800
    ttlSecondsAfterFinished: 600
  concurrency: 2
  resources:
    requests: { cpu: 250m, memory: 512Mi }
    limits: { cpu: "1", memory: 2Gi }
```

Topic grants are written without the `a2a.topics.` prefix, as `shared.{topic}` or `agent.{agent}.{topic}`. Nothing else is accepted.

## What the operator renders

A profile binds to the PlatformAgent in its own namespace. With that agent on `mode: next`, the operator renders three things per profile:

- A ServiceAccount, `agentprofile-<name>`, owned by the profile, with token automount off and no RoleBinding. If `spec.identity.serviceAccountName` names an existing ServiceAccount, that one is used and none is created. Naming a ServiceAccount does hand the profile's pods whatever RBAC it holds.
- A bus identity for that ServiceAccount in the auth callout's identity map. Its pods may publish their own task events, read their own task input, and use the topics the profile names. Nothing else.
- An agent card on `a2a.agents.<name>`, rendered from `spec.description`. Deleting the profile publishes a tombstone in its place before the profile is removed.

A profile may not run as `default`, as a ServiceAccount the operator already uses (for the PlatformAgent or for itself), or as one another `AgentProfile` already holds. Such a profile renders nothing, and its `IdentityReady` condition says why.

Writing an `AgentProfile` grants a bus identity, so treat create and update on `agentprofiles` like create on RoleBindings in that namespace.

Nothing runs a pod from a profile yet. The dispatcher that turns a task into a Job for its profile comes later.

## Status

| Field                       | Meaning                                                                                                                                                                               |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `status.agentRef`           | The PlatformAgent the profile is bound to.                                                                                                                                            |
| `status.serviceAccountName` | The ServiceAccount its pods run as.                                                                                                                                                   |
| `IdentityReady` condition   | The ServiceAccount and bus identity are rendered, or why not (`ModeNotNext`, `NoPlatformAgent`, `MultiplePlatformAgents`, `ServiceAccountRefused`, `ServiceAccountNotFound`).         |
| `CardPublished` condition   | The agent card is on the directory, or why not (`BusUnavailable`, `OperatorBusIdentityUnconfigured`, `IdentityNotRendered`, or the same mode and binding reasons as `IdentityReady`). |
