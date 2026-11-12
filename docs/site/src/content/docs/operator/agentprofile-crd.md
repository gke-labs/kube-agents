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
- **Samples**: [`k8s-operator/examples/agentprofile-chat.yaml`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/examples/agentprofile-chat.yaml), [`k8s-operator/examples/agentprofile-tse.yaml`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/examples/agentprofile-tse.yaml), [`k8s-operator/examples/agentprofile-cluster.yaml`](https://github.com/gke-labs/kube-agents/blob/main/k8s-operator/examples/agentprofile-cluster.yaml) (the generic cluster agent and a pinned variant)
- **Fields**: [AgentProfile fields](/kube-agents/operator/agentprofile-fields/), every `spec` and `status` field with its type, default and constraints, generated from the Go API types.

## Specification

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: AgentProfile
metadata:
  # A dot-free DNS-1123 label: it is the addressee on the bus. The name plus its
  # longest listed tier is at most 54 characters. `platform` is reserved.
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
  identity: {} # absent: the operator creates a ServiceAccount per tier, with no RBAC
  lifecycle:
    activeDeadlineSeconds: 1800
    ttlSecondsAfterFinished: 600
  concurrency: 2
  allowedTiers: [read] # optional; absent means read alone
  parameters: # optional; the task parameters the persona declares
    - name: cluster
      required: true
  resources:
    requests: { cpu: 250m, memory: 512Mi }
    limits: { cpu: "1", memory: 2Gi }
```

The persona image and each tools image keep their content under a `/files` directory. The persona mounts read-only at `/persona`. Each tool mounts read-only at `/opt/tools/<name>`, and its `/opt/tools/<name>/bin` goes on `PATH` in list order, so put a tool's binaries in `/files/bin`. Where the cluster can't mount image volumes (GKE Autopilot, Kubernetes below 1.35), an init container copies `/files` into an emptyDir instead, so these images also need `/bin/sh`. A tools image holds no credentials: a tool that needs them gets them from the broker, per task.

Where an init container copies them, the persona's `/files` and each tool's must fit in 512Mi, and a profile can carry up to 10 tools. More would push a worker pod's ephemeral-storage request past Autopilot's 10Gi per pod, where the pod wouldn't schedule. So on an install without image volumes, the operator renders no warm pool for a profile with more than 10 tools, and writes a `ProfileNamespaceFailed` Warning event on the PlatformAgent that names the profile.

Topic grants are written without the `a2a.topics.` prefix, as `shared.{topic}` or `agent.{agent}.{topic}`. Nothing else is accepted. A grant only works on a topic the install provisions, and the operator does not check that, so a grant on any other topic is refused by the bus at runtime. A profile may read any agent's topics, but the only agent-scoped topics it may publish are its own (`agent.<this profile's name>.{topic}`), because an agent's topics have one writer.

## What the operator renders

A profile binds to the PlatformAgent in its own namespace. With that agent on `mode: next`, the operator renders three things per profile:

- A ServiceAccount per allowed tier, `agentprofile-<name>-<tier>`, in the profile namespace (see below). Each has token automount off and no RoleBinding, and carries the profile's name and UID, since an owner reference can't cross namespaces. If `spec.identity.serviceAccountName` names an existing ServiceAccount in the profile namespace, that one is used and none is created. A named ServiceAccount can serve only one tier, so the CRD refuses it alongside more than one entry in `allowedTiers`. Naming a ServiceAccount does hand the profile's pods whatever RBAC it holds.
- A bus identity per tier in the auth callout's identity map, user `profile-<name>-<tier>`. Its pods may publish task events and read task input for this profile's tasks, take work the dispatcher sends to that pod on `a2a.dispatch.<name>.<tier>.<pod>`, accept it and renew its lease there, and use the topics the profile names. Nothing else. The task-subject scope is the profile, not one task: two pods of the same profile can reach each other's task subjects.
- An agent card on `a2a.agents.<name>`, rendered from `spec.description`. Deleting the profile on a `mode: next` install publishes a tombstone in its place before the profile is removed. A profile deleted while its PlatformAgent is on `today`, while its namespace holds no PlatformAgent or more than one, or while the operator has no bus identity, is removed without one.

When a bound `AgentProfile`'s name matches a built-in optional specialist template baked into the `platform-agent` image (`tse`), the operator also sets `KUBE_AGENTS_ENABLED_PROFILES` on the gateway Deployment (which triggers a gateway pod rollout when `tse` is added or removed so the entrypoint scaffolds or archives `$HERMES_HOME/profiles/tse`). Ordinary `AgentProfile` resources (such as `chat` or `auditor`) do not touch `KUBE_AGENTS_ENABLED_PROFILES` and do not roll the gateway pod.

Deletion waits on the `kubeagents.x-k8s.io/agentprofile-finalizer` finalizer, which every profile reconciled on a `mode: next` install carries and which only the operator removes. Before rolling the operator back to a release without AgentProfiles, or uninstalling it, delete the profiles while the operator is still running. Otherwise `kubectl delete agentprofile` hangs, and so does deleting the CRD behind it. To clear one by hand (it publishes no tombstone, so the card stays on the directory), and then delete its ServiceAccounts in the profile namespace, which carry no owner reference and so outlive it. Left there, they'd make a profile re-created under the same name find them foreign and be refused:

```bash
kubectl patch agentprofile <name> -n <namespace> --type=merge -p '{"metadata":{"finalizers":null}}'
kubectl delete serviceaccount -n <namespace>-profiles -l kubeagents.x-k8s.io/agentprofile=<name>
```

A profile may not name `default`, a ServiceAccount name the operator uses for the PlatformAgent, or one another `AgentProfile` already holds. Such a profile renders nothing, and its `IdentityReady` condition says why.

A `spec.identity.serviceAccountName` is looked up in the profile namespace. A profile written when ServiceAccounts lived in the agent's namespace reports `ServiceAccountNotFound` until that ServiceAccount is created in the profile namespace.

The operator looks for AgentProfiles once, at startup. If the CRD isn't installed, or the operator's ClusterRole predates AgentProfiles (the RBAC self-check reports the missing `agentprofiles` permissions), it skips the AgentProfile controller, renders no profile identities, and keeps running everything else. Restart the operator after installing the CRD or applying the current role.

Writing an `AgentProfile` grants a bus identity, so treat create and update on `agentprofiles` like create on RoleBindings in that namespace. A profile's name is its addressee on the task subjects, the same space the gateway's session pods use. A profile named exactly like a live session pod could write that session's task events, so nobody who can't already act as that session should be able to create profiles.

`allowedTiers` lists the tiers the profile may run at, at most four: the built-in `read` and `unattended`, or a [`Tier`](/kube-agents/operator/tier-crd/) in the install's namespace. A task runs at its requester's tier, and the dispatcher will refuse one whose tier the profile doesn't list. Absent means `read` alone.

A listed tier name is at most 10 lowercase letters or digits, with no hyphen. It becomes part of the profile's bus identity (`profile-<name>-<tier>`, one 63-character label) and of its queue names, so the cap keeps every derived name valid, and the missing hyphen keeps two profiles' queue names from colliding. A longer or hyphenated `Tier` can exist, but no profile can list it.

The operator decides what each listed name renders, and reports it in the `TiersRendered` condition:

- **A name with no tier** renders nothing (reason `TierUnknown`). The profile's other tiers render as usual, and a profile left with none renders nothing at all.
- **At most one unattended-mode tier** per profile, because unattended tasks run as Jobs from one template per profile. If a profile lists a second one, the one it already serves keeps serving and the newcomer renders nothing (reason `TierModeConflict`). A new profile listing two renders neither until one is removed.
- **A tier dropped from the list drains.** That covers one whose `Tier` is deleted, and one that stops rendering. The card stops advertising it, and nothing new starts at it at once. A task already queued at it is refused with the reason `tier-withdrawn`. A task already running at it keeps its ServiceAccount, queues and pool, and finishes. The tier drains for the longest deadline a task can carry plus 15 minutes, then everything it rendered is removed within a few minutes. `TiersRendered` names each draining tier and when its drain ends.

  Listing the tier again ends the drain once it renders again: not while its `Tier` is missing, and not while it conflicts with the profile's live unattended-mode tier. A running task that delegates to its own profile at the draining tier is refused like any new task; delegating to another profile still works. A profile left with no tier that renders drains as a removed profile does. A profile on a named ServiceAccount (`spec.identity`) serves one tier, so its tier is replaced at once rather than drained.

  **This is not an emergency revoke.** Deleting a `Tier`, or dropping it from a profile, doesn't cut off work already running at it: that work carries on until the drain ends. To stop it now, use the stop (or the dispatcher's breaker).

`status.renderedTiers` records what the last pass rendered. The operator uses it to tell the tier a profile already serves from a newcomer. If status is lost (a restore, a GitOps apply), it reads the profile's own ServiceAccounts once instead.

`parameters` lists the task parameters the profile's persona declares, at most 16, each a lower-case identifier the worker exports as `PROFILE_TASK_<NAME>`. The gateway and the dispatcher read it here because neither can read the persona image: the gateway sends cluster work to a profile that declares `project`, `location` and `cluster`, and the dispatcher refuses a submission missing a `required` one. The list must match the persona's, and a worker refuses to run a task when they differ.

Profile workers run in their own namespace, `<agent-namespace>-profiles`, which the chart creates on a `mode: next` install. The operator never creates it, and treats a namespace of that name as the profile namespace only when it carries the chart's `app.kubernetes.io/component: profiles` label and its `meta.helm.sh/release-namespace` annotation naming the agent's namespace. Into it the operator renders a pod quota and a NetworkPolicy. That policy lets its pods reach DNS, the bus, LiteLLM and the credential broker's port, and nothing else.

Deleting the PlatformAgent removes its quota from that namespace. If the delete is refused, the operator retries for two minutes and then lets the PlatformAgent go, leaving the quota and a `ProfileQuotaLeft` Warning event that names it. A later PlatformAgent in the same namespace removes the leftover quota when it renders its own (a `ProfileQuotaAdopted` event); one of the same name takes it over as its own. The NetworkPolicy stays until the chart's uninstall deletes the namespace, since removing it first would leave the namespace admitted to the bus without its own policy.

Only once that policy is in place does the operator admit the namespace on the bus's and LiteLLM's own policies. The broker admits it on its own policy once a bound profile has a ServiceAccount for it to serve. LiteLLM is reached without a key, so anyone who can create a pod in that namespace can use the install's model credentials. Treat pod create there like pod create in the agent's own namespace.

The profile's ServiceAccounts live there too. While the namespace is missing, or doesn't carry the chart's markers, a profile renders no identity and withdraws its card, and `IdentityReady` says `ProfileNamespaceAbsent`.

The dispatcher, `<agent>-a2a-dispatcher`, reads the profiles' submissions and hands each to a ready worker. It is the only component that publishes directly to a profile task's supervisor subject, because the bus's config denies the A2A gateway each profile's by name. It's off by default until profiles dispatch end to end: a `mode: next` install runs it only when the operator's `A2A_DISPATCHER_IMAGE` names its image.

Binding or removing a profile, or one finishing its drain, changes the bus's config, whether or not the dispatcher runs. The bus reloads it in place, without a restart, usually within a minute. Session pods' bus connections drop and reconnect at the reload, within a second, and their tasks carry on. If the reload can't be confirmed within three minutes, the operator restarts the bus pod instead and records a `BusConfigReloadFailed` Warning on the PlatformAgent. A session task running at that moment fails, as on any bus restart.

With the dispatcher on, each admitted profile also gets a warm pool for each of its interactive tiers: a Deployment `agentprofile-<profile>-<tier>` in the profile namespace, running `warmReplicas` worker pods plus one surge pod during a rollout. The profile quota budgets those pods. The pods hold the profile's bus and broker tokens, and the operator copies the bus CA bundle beside them. A worker empties its writable directories after every task, so nothing one task leaves reaches the next. Turning the dispatcher off, or removing or refusing the profile, removes its pools.

## Status

[AgentProfile fields → Status](/kube-agents/operator/agentprofile-fields/#status) lists the status fields. The conditions carry the detail:

| Condition                 | Meaning                                                                                                                                                                                                                                                                                                                                                                                                          |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `IdentityReady` condition | The ServiceAccount and bus identity are rendered, or why not (`ModeNotNext`, `ModeNotRecognized`, `NoPlatformAgent`, `MultiplePlatformAgents`, `InvalidProfile`, `ServiceAccountRefused`, `ServiceAccountNotFound`, `ProfileNamespaceAbsent`, `TierUnknown` or `TierModeConflict` when no listed tier renders, `DispatchConfigFull` when the profiles created before it fill the dispatch config's byte budget). |
| `TiersRendered` condition | Every listed tier renders (`AllTiersRendered`), or which don't and why (`TierUnknown`, `TierModeConflict`). Its message names the tiers that render.                                                                                                                                                                                                                                                             |
| `CardPublished` condition | The agent card is on the directory, or why not (`BusUnavailable`, `OperatorBusIdentityUnconfigured`, `IdentityNotRendered`, or the same mode and binding reasons as `IdentityReady`).                                                                                                                                                                                                                            |
