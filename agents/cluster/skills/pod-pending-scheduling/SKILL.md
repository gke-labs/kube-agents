---
name: pod-pending-scheduling
description: Finds why a pod stays Pending and cannot be scheduled. Use for Pending pods, FailedScheduling events, "Insufficient cpu" or "Insufficient memory", untolerated taints, node affinity or nodeSelector mismatches, unbound PersistentVolumeClaims at scheduling, ResourceQuota limits and Autopilot ComputeClass placement. Proposes the manifest or capacity fix.
---

# Task

Name why one pod cannot be placed, quote the scheduler's message, and propose the fix. Replicas of one controller share a cause: read one.

# Workflow

1. Read the pod's placement constraints and scheduling condition in one projected call:

   ```bash
   kubectl get pod <pod> -n <namespace> -o json | jq '{owner: .metadata.ownerReferences, nodeSelector: .spec.nodeSelector, affinity: .spec.affinity, tolerations: .spec.tolerations, priorityClassName: .spec.priorityClassName, requests: [.spec.containers[] | {name, requests: .resources.requests}], volumes: [.spec.volumes[]? | select(.persistentVolumeClaim) | .persistentVolumeClaim.claimName], conditions: .status.conditions}'
   ```

2. Read the scheduler's events for the pod:

   ```bash
   kubectl get events -n <namespace> --field-selector involvedObject.name=<pod> -o custom-columns=LAST:.lastTimestamp,REASON:.reason,COUNT:.count,MESSAGE:.message
   ```

3. Read the nodes' pools, taints and allocatable capacity, only if the message names resources, taints or affinity, or if a `NotTriggerScaleUp` event gives no reason:

   ```bash
   kubectl get nodes -o custom-columns='NAME:.metadata.name,POOL:.metadata.labels.cloud\.google\.com/gke-nodepool,CLASS:.metadata.labels.cloud\.google\.com/compute-class,TAINTS:.spec.taints[*].key,CPU:.status.allocatable.cpu,MEMORY:.status.allocatable.memory'
   ```

   When a `NotTriggerScaleUp` event gives no reason, also read the namespace's labels, and the cluster's version and node pools:

   ```bash
   kubectl get namespace <namespace> -o jsonpath='{.metadata.labels}{"\n"}'
   gcloud container clusters describe <cluster_name> --location <cluster_location> --project <project_id> --format="json(currentMasterVersion,nodePools[].name,nodePools[].locations,nodePools[].config.labels,nodePools[].autoscaling)"
   ```

4. Read quota or claims only if the message names them:

   ```bash
   kubectl get resourcequota,pvc -n <namespace> -o wide
   ```

5. Record the finding as [`workload-triage-scan/references/reporting.md`](../workload-triage-scan/references/reporting.md) describes. Stop once the cause, the evidence and any applicable patch are written. When no manifest fix applies (an active scale-up, or a `gone`/`-` warning row whose replacement pod is already running), report the confirming evidence with no proposed patch.

# Signatures

| Scheduler message                                     | Cause                                         | Patch                                                                    |
| ----------------------------------------------------- | --------------------------------------------- | ------------------------------------------------------------------------ |
| `Insufficient cpu`, `Insufficient memory`             | Requests exceed free allocatable capacity     | Lower requests to real usage, or enlarge or autoscale the node pool      |
| `had untolerated taint(s)`                            | Pod lacks the toleration (Spot, GPU, system)  | Add the matching toleration, or target an untainted pool                 |
| `didn't match Pod's node affinity/selector`           | Selector or affinity names no existing node   | Correct the label, or create the pool it names                           |
| `persistentvolumeclaim "<name>" not found`, `unbound` | Missing or unprovisioned claim                | Create the claim; for a provisioning error load `pod-container-creating` |
| `exceeded quota`                                      | Namespace ResourceQuota reached               | Raise the quota or lower requests                                        |
| `pod triggered scale-up`, `TriggeredScaleUp`          | Cluster autoscaler or Autopilot adding a node | None: report the pod as initializing                                     |
| `max node group size reached`, `ScaleUpFailed`        | Node pool at its autoscaling ceiling          | Raise the pool's max nodes, or lower requests                            |
| `Pod didn't trigger scale-up:` and no reason after it | Likely a ComputeClass pre-filter skip         | Name the class in the pod template (see the note below); no larger pool  |

On Autopilot, a ComputeClass the pod selects that cannot be provisioned shows in the events; quote that event.

A repeated `NotTriggerScaleUp` event with the message `Pod didn't trigger scale-up:` and no reason after the colon can be a ComputeClass pre-filter skip: the Cluster Autoscaler does not scale up a pool that has a `cloud.google.com/compute-class` label for a pod that does not name that class. Rule it out before you stop at `Insufficient cpu` or propose a larger pool. It is the likely cause when all of these are true:

- The pod names no ComputeClass: `cloud.google.com/compute-class` is not in its `nodeSelector` and not in a required `nodeAffinity` term. Both count as naming the class.
- A default class on the namespace (the label `cloud.google.com/default-compute-class` or `cloud.google.com/default-compute-class-non-daemonset`) counts only when the pod's `nodeSelector` shows the class: GKE adds it to pods that are created after the label. A label that was set after the pod was created does not rule the skip out.
- Each node pool with autoscaling on has the class label in `config.labels`, and has room to grow. `autoscaling.maxNodeCount` is for each zone: compare the pool's node count with `autoscaling.totalMaxNodeCount` when it is set, else with `maxNodeCount` times the number of the pool's zones (`locations`).
- The event is repeated. One event with no reason is not enough: the autoscaler can write one before it gives a reason, such as `1 max node group size reached`.

Then:

- State the version (`currentMasterVersion`). The skip was seen on GKE 1.35.8 and was not seen on GKE 1.36.4. On another version, say that it is not confirmed there.
- Report the cause as likely. These steps do not read the autoscaler's visibility log or a cluster default class (a ComputeClass with the name `default`): name both as the reads that confirm it.
- Give `cloud.google.com/compute-class: <name>` in the pod template's `nodeSelector` as the patch. A namespace default-class label is the other way; it applies to new pods only, so pods that were Pending before the label must be created again.
- Do not call it a capacity shortage, and do not propose a larger pool or lower requests.

# Rules

- Budget about five commands.
- When routed from a `WARNING EVENTS` row whose pod is `gone` (deleted after rollback, scale-down or preemption) or whose object is not a pod (`STATE` `-`), skip step 1 (`kubectl get pod`): read step 2's events for that object's `involvedObject.name` (plus the owning workload's current pods for a `gone` pod if needed to confirm recovery) and proceed from step 3 or step 4 as the event message requires.
- A scale-up in progress is reported, never waited on: no `sleep`, no `--watch`.
- Do not run `gcloud container clusters get-credentials`. Preflight has pinned the kubeconfig.
