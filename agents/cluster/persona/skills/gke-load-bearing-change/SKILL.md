---
name: gke-load-bearing-change
description: Establish whether an out-of-band change to a live object is load-bearing — whether the state the manifests declare would still run if the change were undone — before recommending a revert. Read-only. For drift triage, emergency hotfixes found in a cluster, and any decision between a declared state and a live one.
---

# GKE Load-Bearing Change

Use this skill when something changed a live object outside git and you have to say whether the declared state or the live state should win. It answers one question, and that question decides the recommendation: **would the state git declares still run if you put it back?** A change that is the only reason a workload is up is load-bearing, and proposing to revert it proposes an outage. A change that nothing depends on is not, and proposing to codify it writes somebody's accident into git.

The skill is read-only: it reads history, events, the live object and whatever depends on it, and proposes nothing beyond naming what it found.

It does not try to learn why the change was made. An operator unblocking a deploy at speed leaves no annotation and no ticket, so the motive is usually unknowable, and guessing at one is how a report ends up asserting something it cannot support. Load-bearing is a property of the cluster, not of anybody's intent, and it is observable.

## When to run it

- A drift request asks you to choose between reverting an out-of-band change and codifying it.
- Any time the recommendation you are about to write is "put it back the way the manifests say", and the manifests have not been applied successfully since the change.
- A limit, a probe, a replica count, a policy or a hardening setting is looser or absent in the cluster compared with git, and the workload is healthy. Health under the changed state is the first hint that the change is doing something.

Skip it when the live object already matches the declared state: a GitOps controller reconciled the change away and there is nothing left to decide.

## First: does git declare this object at all?

Everything below assumes git holds a copy of the object to put back. Check that first: the drift detector forwards a change to an object nobody ever put in git exactly as it forwards one to an object a GitOps tool syncs.

**This needs a live object.** A deleted object carries nothing to read, so no markers is no evidence there: say whether git declares it could not be read off the cluster, and go on to the method with the revert still an option. The same holds when the object is gone by the time you look.

You cannot read the repository, so read what a GitOps tool leaves on the objects it applies:

| Tool        | Field manager in `managedFields`          | Label or annotation                                                            |
| ----------- | ----------------------------------------- | ------------------------------------------------------------------------------ |
| Argo CD     | `argocd-controller`                       | annotation `argocd.argoproj.io/tracking-id`                                    |
| Flux        | `kustomize-controller`, `helm-controller` | labels `kustomize.toolkit.fluxcd.io/name`, `helm.toolkit.fluxcd.io/name`       |
| Config Sync | `configsync.gke.io`                       | annotations `configmanagement.gke.io/managed`, `configsync.gke.io/resource-id` |

Read the managers and the markers by name rather than dumping every annotation: `last-applied-configuration` on a Secret or ConfigMap carries its data.

```bash
kubectl get <kind> <name> [-n <namespace>] -o jsonpath='{range .metadata.managedFields[*]}{.manager}{" "}{end}{"\n"}{.metadata.labels}{"\n"}{.metadata.annotations.argocd\.argoproj\.io/tracking-id}{"\n"}{.metadata.annotations.configmanagement\.gke\.io/managed}'
```

- **Any of them present:** a tool syncs it from git. Go on to the method.
- **Only an ambiguous one:** `kubectl-client-side-apply` as a manager, the labels `app.kubernetes.io/instance` and `app.kubernetes.io/managed-by: Helm`, or a field manager this table does not name and that is not a person's `kubectl-*` subcommand, which may be a pipeline's own. A person's `kubectl apply -f` and `helm install` leave the first two too, and so can Argo CD under client-side sync or label tracking. Say the object was applied from a manifest, a chart or a tool whose source the cluster does not show, name the manager, and keep both the revert and the options below, saying which depends on where that source lives.
- **None present, and every field manager is a person's `kubectl-*` subcommand** (`kubectl-create`, `kubectl-edit`, `kubectl-label`, `kubectl-patch`, `kubectl-replace`, `kubectl-annotate`): say the object is probably not declared in git, and list what you checked. Probably, not certainly: a pipeline that only ever creates and patches imperatively leaves the same managers.

An object git does not declare has no declared state, so there is nothing to revert to, and the method below does not apply. Do not offer "revert to the declared state". The choices are:

- **Codify it.** This is the option the request letters; it is the only one a pull request can carry. Add a manifest for it to git. Say whether the manifest keeps the change or undoes it: weigh that the way "What you conclude" weighs a revert against a codify, so a loosening nothing depends on is codified with the change undone, and one something depends on is codified as it is now, with its durable fix named.
- **Delete it.** Git declares nothing, so deleting it brings the cluster in line with git. No pull request can delete an object git never held, so it is a manual step a person runs, never a lettered option and never the recommendation: a lettered option becomes an "apply" button on Slack. Where the evidence says the object should not exist (nothing uses it, nothing suggests it was meant to), name it in the one unlettered bullet the request describes, `- **Manual step (<title>):**`, with the command, and do not call it an option.

Do not offer to undo the change by hand. Relabelling or patching it back leaves the object as unmanaged as before, which is the out-of-band fix the request rules out.

## The method

Four steps, and they do not depend on what kind of object changed. Step 3 is where the evidence location varies, and the table below is for that step.

**1. Establish what the revert would actually put back.** Not the one field the request names — the whole declared state as it would land. Two fields are often taken together (a `runAsNonRoot` and the `runAsUser` beside it), and a revert that restores one without the other produces a third state nobody has ever run.

**You have no GitOps repository to read, and you are not meant to find one.** You hold a read-only view of one cluster — no checkout, no clone, no forge access. Do not go looking for one.

**Read `managedFields` for ownership, and know what it cannot give you.** It records which paths each manager owns and never the value they held, so it tells you which fields the out-of-band manager took and which the GitOps manager still owns, and nothing about what the revert would restore:

```bash
kubectl get <kind> <name> -n <namespace> --show-managed-fields -o yaml
```

Narrow that read when the object can hold a credential. A Secret, and a ConfigMap you have not checked, go through `-o jsonpath` for the fields you need, or `-o jsonpath='{.metadata.managedFields}'` for ownership alone: your report is posted verbatim into a chat thread, and a dumped value is a leaked one.

**Whether the declared value itself is readable depends on what changed:**

| What changed                                                                                                                         | The declared value, from this cluster                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| ------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| A pod-template field                                                                                                                 | Readable. A retired ReplicaSet or ControllerRevision still carries the template.                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| Anything outside the pod template — `spec.replicas`, a ConfigMap or Secret value, a NetworkPolicy, PDB, quota, RBAC binding, webhook | Usually readable, from `kubectl.kubernetes.io/last-applied-configuration`. No revision is cut for these and `managedFields` gives the owner and the path rather than the value, but a GitOps manager doing a **client-side** apply writes the whole declared object into that annotation — and client-side is Argo CD's default sync, so this is the common case. A **server-side** apply writes no annotation: that is Flux's kustomize-controller, and Argo CD only with the `ServerSideApply=true` sync option. Check before concluding it is unreadable. |

**The request tells you which apply mode you are in.** Its ownership block renders each manager with an `operation` qualifier: `Apply` is a server-side apply, `Update` is everything else — client-side `kubectl apply` included. A GitOps manager shown as `Update` wrote the annotation; read it before concluding anything:

```bash
kubectl get <kind> <name> -n <namespace> \
  -o jsonpath='{.metadata.annotations.kubectl\.kubernetes\.io/last-applied-configuration}'
```

**Only when it is genuinely not readable, report that; do not guess it.** "What the revert would put back could not be read from this cluster — `managedFields` shows `<manager>` owns `<path>`, no revision carries the value and there is no `last-applied-configuration`" is a complete answer to this step, and it does not stop the rest. Reach it by having looked, not by assuming. The reader's decision is still revert-or-codify, and neither option needs the number: one restores whatever git declares, the other writes what is live. What you lose is the revert's _cost_, so name that as the gap rather than inventing a figure to fill it.

**2. Ask whether the declared state has ever been observed running.** This is the question. It is answered either by a recorded failure (something tried it and it did not work) or by a derivation from the spec (it cannot work, and here is why). Both are good answers; a derivation does not expire and a recording is more persuasive, so take both when both are there.

**3. Look where the evidence for that lives.** See the table.

**4. Conclude one of three** — load-bearing, not load-bearing, or not established — and say which, with what you read. See "What you conclude".

## Where the evidence lives, by what changed

| What changed                                                    | The recorded failure                                                                                                                                                                              | The derivation from the spec                                                                                                                                                                                                                                                                                                                                                         |
| --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| A pod-template field (securityContext, capabilities, user)      | A retired ReplicaSet still carrying the declared value; kubelet refusals on the pods it made (`container has runAsNonRoot and image will run as root`, surfacing as `CreateContainerConfigError`) | The container's own `command`, entrypoint and mounts: a process writing outside its volumes needs a uid that can write there                                                                                                                                                                                                                                                         |
| Resource requests or limits                                     | `lastState.terminated.reason: OOMKilled` on a current pod, restart counts, `OOMKilling` and `Evicted` events, the retired revision's limit value                                                  | The workload's steady-state usage against the declared limit (`kubectl top pod`), and the limit's headroom over its own requests                                                                                                                                                                                                                                                     |
| Replica count                                                   | What the pods did before the change — `FailedScheduling` if the cluster could not fit the count, saturation or restarts if it could not carry the load                                            | Whether an HPA owns the field (`kubectl get hpa -n <namespace>`); if one does, git declaring a replica count at all is the finding and neither revert nor codify is the answer. Otherwise the running pods' utilisation and whether the workload has headroom to lose replicas — the declared count itself is usually unreadable here, so reason about direction, not about a number |
| A probe loosened, lengthened or removed                         | Restart counts and `Unhealthy` events from before the change; the retired revision's probe values                                                                                                 | How long the container actually takes to serve its probe path, against the declared `initialDelaySeconds` and `failureThreshold`                                                                                                                                                                                                                                                     |
| An image tag or digest                                          | The retired revision running the declared image and what it did; `ImagePullBackOff`, `CrashLoopBackOff` with its exit code                                                                        | Whether the declared tag still resolves at all                                                                                                                                                                                                                                                                                                                                       |
| A NetworkPolicy, PDB, quota or RBAC object changed or deleted   | Consumer-side symptoms before the change: connection timeouts in a client's logs, `FailedCreate` on a controller, evictions blocked by the PDB                                                    | Which pods the object selected and what they talk to; a ResourceQuota's `status.used` against the `hard` now live, which says how much room the current limit is giving                                                                                                                                                                                                              |
| A ConfigMap or Secret value                                     | The consuming workload's restart and health history around the change timestamp                                                                                                                   | What the consumer does with the key — the declared value's effect is readable in the consumer's spec, not in the ConfigMap                                                                                                                                                                                                                                                           |
| A webhook or admission config (`failurePolicy`, a rule removed) | Admission failures on unrelated objects before the change (`failed calling webhook`)                                                                                                              | Whether the webhook's backing Service has ready endpoints; `failurePolicy: Fail` against a dead backend blocks every matching admission                                                                                                                                                                                                                                              |

Across all of them the ordering is the same: the live object's current health is the cheapest read and sets up the comparison; events are the richest and the first to go; history and spec derivations are what survive a request worked hours later.

**Events expire after about an hour.** Their absence says nothing at all. Never write "no events show a problem, so the declared state is fine" — that sentence is how this goes wrong.

## Reading rollout history

For anything with revisions, the history is the evidence that does not decay.

```bash
kubectl rollout history deployment/<name> -n <namespace>
kubectl rollout history deployment/<name> -n <namespace> --revision=<n>
kubectl get rs -n <namespace> -l <selector> \
  -o custom-columns='NAME:.metadata.name,REVISION:.metadata.annotations.deployment\.kubernetes\.io/revision,CREATED:.metadata.creationTimestamp,DESIRED:.spec.replicas,READY:.status.readyReplicas'
```

Every superseded revision keeps its pod template on a retired ReplicaSet. Three things to read out of it:

- **Which revisions carry the declared values.** A retired ReplicaSet still holding what git declares is the declared state, recorded, with the fact that the cluster moved off it.
- **The spacing.** Several revisions minutes apart, each looser than the last, is somebody fighting to get the workload up. The same revisions weeks apart are ordinary deployment cadence. Compare `creationTimestamp` against the change timestamp in the request.
- **Whether the obvious repair was already tried.** If a middle revision shows the compromise you were about to propose as a third option — keep the hardening, add an explicit uid; keep the limit, raise the request — the cluster has already answered it. Say that instead of proposing it.

**A current status field is not a historical fact.** `readyReplicas` is omitted when it is zero, so a revision that ran perfectly well before being superseded reads exactly like one that never started, and `READY` of 0 or empty on a retired ReplicaSet proves nothing on its own. The same trap applies to restart counts reset by a delete, to `status.conditions` that only describe now, and to `kubectl top`, which is a current sample. What carries a historical claim is the template a revision holds, when it was cut, and a recorded event or a spec derivation about it.

StatefulSets and DaemonSets expose the same history through `kubectl rollout history`; their retired revisions are ControllerRevisions, which `kubectl get controllerrevision -n <namespace>` lists. A `revisionHistoryLimit` lowered below the number of revisions you need is missing evidence, not evidence of absence — say so rather than concluding from what is left.

## Objects with no history

A Service, a NetworkPolicy, a ConfigMap, an HPA, a quota, a webhook or an RBAC binding has no revisions to read, so the question becomes what depended on it. Name the consumers and read their state: the pods the policy selected, the endpoints behind the Service, the workload that mounts the ConfigMap, the Deployment the HPA scales, the principal the binding grants.

A change to one of these is load-bearing when undoing it would break a consumer that is working now, and the evidence is that consumer's state plus the connection between them — not the object's own history, which does not exist.

**A deletion is the common case here and it is usually not load-bearing.** Something being gone is not evidence that its absence is holding anything up; most deleted policy objects were deleted by mistake or by a stale apply, and the revert is correct. Establish the dependency positively or conclude that nothing depends on it.

## What you conclude

Say which of the three it is, in the report, with what you read.

- **Load-bearing.** The declared state does not run and you have the evidence. The recommendation is the option that leaves the workload running — write the live state into git so git stops declaring something the cluster cannot start. State the revert as an option anyway, with its cost named: the workload stops starting.
- **Not load-bearing.** Both states run, or the declared state ran until the change and nothing about the workload has changed since. The recommendation is the revert, and the live state is the option that carries the cost.
- **Not established.** The events expired, the history was trimmed, the spec gives you nothing. Say so, and say what was missing: "whether the declared state runs could not be established from the cluster, because <what was missing>". An unestablished answer is a result. Recommending the revert because nothing contradicted it is how a correct-looking report takes a service down.

Two qualifications on the first one.

**Load-bearing is not an argument that the live state is good.** A workload that only runs as root should get an image that does not need root; a limit raised to stop OOMKills may be hiding a leak; `failurePolicy: Ignore` keeps admissions flowing by not checking them. Where the live state is holding something up and should not be the permanent answer, name the durable fix alongside the recommendation and keep the recommendation itself to what the reader can authorize today.

**A declared state that was never applied is a different finding.** If the object is not managed by the GitOps controller at all, or the controller has not synced since before the change, then "revert to declared" means applying something that has never run anywhere in this cluster. The field managers on the live object are how you tell. Say that rather than treating the declared state as a known-good baseline.

## Recording the finding

You work under a strict read-only boundary: no patches applied, no Pull Requests opened, nothing written to the cluster. The fix ships as a Pull Request against the GitOps repository and somebody else opens it — undoing an out-of-band write with another out-of-band write leaves the cluster no closer to git.

A request that gives a report format wins: format your answer exactly as the request says, including the parts of its shape it makes conditional — a drift request from the Session KV server's `gitops-drift` inject asks for lettered options and a `Recommended:` line only where there are two or more options, and drops the `To authorize:` bullet entirely where the change was already reconciled away. Follow the request on all of that.

What this skill decides is the content, not the shape. The evidence belongs in `Why`, quoted — the revision that carries the declared template, the kubelet's own sentence, the container's command, the consumer that would break — and the conclusion above decides which option is recommended where the request asks for one.

Without a format from the request, follow the shape in `gke-workload-troubleshooting`, with the classification and its evidence leading the answer.
