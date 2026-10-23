---
title: AgentProfile CRD
description: Custom resource describing one kind of A2A agent pod, and what the operator renders from it under mode next.
sidebar:
  order: 3
---

The `AgentProfile` custom resource describes one kind of agent pod on the A2A bus: its persona, worker and tools images, its topic grants, the ServiceAccount it runs as, and how long one task may run. It is part of the `spec.mode: next` stack. On a PlatformAgent running `today` an `AgentProfile` renders nothing.

An `AgentProfile` is not a Hermes profile on its own: the platform and cluster templates the gateway scaffolds under `$HERMES_HOME/profiles` are unaffected by ordinary profiles. The one exception is a built-in optional specialist template shipped in the `platform-agent` image (`tse`, under `/opt/tse-template`): when a bound `AgentProfile` of that name resolves under `mode: next`, the operator also passes its name in `KUBE_AGENTS_ENABLED_PROFILES` on the gateway Deployment so the entrypoint scaffolds and syncs `$HERMES_HOME/profiles/tse` on startup (and archives it to `$HERMES_HOME/profiles/.deleted/tse` when the `AgentProfile` is removed).

- **API group / version**: `kubeagents.x-k8s.io/v1alpha1`
- **Kind**: `AgentProfile`
- **Short Name**: `apr`
- **Source**: [`k8s-operator/api/v1alpha1/agentprofile_types.go`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/api/v1alpha1/agentprofile_types.go)
- **Samples**: [`k8s-operator/examples/agentprofile-chat.yaml`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/examples/agentprofile-chat.yaml), [`k8s-operator/examples/agentprofile-tse.yaml`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/examples/agentprofile-tse.yaml)

## Specification

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: AgentProfile
metadata:
  # A dot-free DNS-1123 label of at most 55 characters: it is the addressee on the bus.
  # `platform` is reserved.
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
  tools: # optional
    - name: jq # at most 24 characters
      image: registry.example.com/kube-agents/tools-jq:1.7
      # imagePullPolicy: IfNotPresent
  bus:
    publishTopics:
      - shared.annotations
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

The persona image and each tools image keep their content under a `/files` directory. The persona mounts read-only at `/persona`. Each tool mounts read-only at `/opt/tools/<name>`, and its `/opt/tools/<name>/bin` goes on `PATH` in list order, so put a tool's binaries in `/files/bin`. Where the cluster can't mount image volumes (GKE Autopilot, Kubernetes below 1.35), an init container copies `/files` into an emptyDir instead, so these images also need `/bin/sh`. A tools image holds no credentials: a tool that needs them gets them from the broker, per task.

Topic grants are written without the `a2a.topics.` prefix, as `shared.{topic}` or `agent.{agent}.{topic}`. Nothing else is accepted. A grant only works on a topic the install provisions, and the operator does not check that, so a grant on any other topic is refused by the bus at runtime. A profile may read any agent's topics, but the only agent-scoped topics it may publish are its own (`agent.<this profile's name>.{topic}`), because an agent's topics have one writer.

## What the operator renders

A profile binds to the PlatformAgent in its own namespace. With that agent on `mode: next`, the operator renders three things per profile:

- A ServiceAccount, `agentprofile-<name>`, owned by the profile, with token automount off and no RoleBinding. If `spec.identity.serviceAccountName` names an existing ServiceAccount, that one is used and none is created. Naming a ServiceAccount does hand the profile's pods whatever RBAC it holds.
- A bus identity for that ServiceAccount in the auth callout's identity map. Its pods may publish task events and read task input for this profile's tasks, and use the topics the profile names. Nothing else. The scope is the profile, not one task: two pods of the same profile can reach each other's task subjects.
- An agent card on `a2a.agents.<name>`, rendered from `spec.description`. Deleting the profile on a `mode: next` install publishes a tombstone in its place before the profile is removed. A profile deleted while its PlatformAgent is on `today`, while its namespace holds no PlatformAgent or more than one, or while the operator has no bus identity, is removed without one.

When a bound `AgentProfile`'s name matches a built-in optional specialist template baked into the `platform-agent` image (`tse`), the operator also sets `KUBE_AGENTS_ENABLED_PROFILES` on the gateway Deployment (which triggers a gateway pod rollout when `tse` is added or removed so the entrypoint scaffolds or archives `$HERMES_HOME/profiles/tse`). Ordinary `AgentProfile` resources (such as `chat` or `auditor`) do not touch `KUBE_AGENTS_ENABLED_PROFILES` and do not roll the gateway pod.

Deletion waits on the `kubeagents.x-k8s.io/agentprofile-finalizer` finalizer, which every profile reconciled on a `mode: next` install carries and which only the operator removes. Before rolling the operator back to a release without AgentProfiles, or uninstalling it, delete the profiles while the operator is still running. Otherwise `kubectl delete agentprofile` hangs, and so does deleting the CRD behind it. To clear one by hand (it publishes no tombstone, so the card stays on the directory):

```bash
kubectl patch agentprofile <name> -n <namespace> --type=merge -p '{"metadata":{"finalizers":null}}'
```

A profile may not run as `default`, as a ServiceAccount the operator already uses (for the PlatformAgent or for itself), or as one another `AgentProfile` already holds. Such a profile renders nothing, and its `IdentityReady` condition says why.

The operator looks for AgentProfiles once, at startup. If the CRD isn't installed, or the operator's ClusterRole predates AgentProfiles (the RBAC self-check reports the missing `agentprofiles` permissions), it skips the AgentProfile controller, renders no profile identities, and keeps running everything else. Restart the operator after installing the CRD or applying the current role.

Writing an `AgentProfile` grants a bus identity, so treat create and update on `agentprofiles` like create on RoleBindings in that namespace. A profile's name is its addressee on the task subjects, the same space the gateway's session pods use. A profile named exactly like a live session pod could write that session's task events, so nobody who can't already act as that session should be able to create profiles.

Nothing runs a pod from a profile yet. The dispatcher that turns a task into a Job for its profile comes later.

## Status

| Field                       | Meaning                                                                                                                                                                                                              |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `status.agentRef`           | The PlatformAgent the profile is bound to.                                                                                                                                                                           |
| `status.serviceAccountName` | The ServiceAccount its pods run as.                                                                                                                                                                                  |
| `IdentityReady` condition   | The ServiceAccount and bus identity are rendered, or why not (`ModeNotNext`, `ModeNotRecognized`, `NoPlatformAgent`, `MultiplePlatformAgents`, `InvalidProfile`, `ServiceAccountRefused`, `ServiceAccountNotFound`). |
| `CardPublished` condition   | The agent card is on the directory, or why not (`BusUnavailable`, `OperatorBusIdentityUnconfigured`, `IdentityNotRendered`, or the same mode and binding reasons as `IdentityReady`).                                |
