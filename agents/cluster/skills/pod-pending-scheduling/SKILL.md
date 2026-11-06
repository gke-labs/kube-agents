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

3. Read the nodes' pools, taints and allocatable capacity, only if the message names resources, taints or affinity:

   ```bash
   kubectl get nodes -o custom-columns='NAME:.metadata.name,POOL:.metadata.labels.cloud\.google\.com/gke-nodepool,CLASS:.metadata.labels.cloud\.google\.com/compute-class,TAINTS:.spec.taints[*].key,CPU:.status.allocatable.cpu,MEMORY:.status.allocatable.memory'
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

On Autopilot, a ComputeClass the pod selects that cannot be provisioned shows in the events; quote that event.

# Rules

- Budget about five commands.
- When routed from a `WARNING EVENTS` row whose pod is `gone` (deleted after rollback, scale-down or preemption) or whose object is not a pod (`STATE` `-`), skip step 1 (`kubectl get pod`): read step 2's events for that object's `involvedObject.name` (plus the owning workload's current pods for a `gone` pod if needed to confirm recovery) and proceed from step 3 or step 4 as the event message requires.
- A scale-up in progress is reported, never waited on: no `sleep`, no `--watch`.
- Do not run `gcloud container clusters get-credentials`. Preflight has pinned the kubeconfig.
