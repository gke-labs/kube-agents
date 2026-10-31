---
name: gke-rightsizing-checks
description: Five required checks, and the arithmetic, before any proposal to lower a workload's CPU or memory requests or limits, or to change a node pool's machine type or size, and before any claim of capacity freed. Read-only. For "lower the requests of X", "right-size this deployment", "can this pool use a smaller machine type", and any answer that claims nodes, CPU, memory or a percentage freed.
---

# GKE Rightsizing Checks

Use this skill before you propose a lower request or limit, a machine type or a node count, and before you claim capacity freed. Raising a limit to stop an OOMKill is a fix, not a rightsizing proposal, and this skill does not apply to it. A request is rarely just a number: an HPA scales on it, a limit may be tied to it, a budget or a claim may share its pods, and the file that declares it may render other clusters too. A proposal that skips those reads looks right and changes something nobody asked to change.

The skill is read-only. It produces checks and arithmetic, and a proposal the person applies through the Platform Agent.

## The rule

Run all five checks for each workload or pool you propose to change, and show each one in the answer: what you checked, the command, and what it returned. A check you could not run is reported as not run, with the reason, and you propose no number for the dimension it governs. Every effect claim (nodes freed, CPU or memory freed, a percentage, "fits without scale-up") carries its calculation, with the inputs it used. Give no prices: this deployment has no price data, and a dollar figure here is a guess.

Set the context once:

```bash
gcloud container clusters get-credentials <cluster_name> --region <cluster_location>
```

## 1. HPA or KEDA on the target

```bash
kubectl get hpa -n <namespace> -o jsonpath='{range .items[*]}{.metadata.name} {.spec.scaleTargetRef.kind}/{.spec.scaleTargetRef.name} min={.spec.minReplicas} max={.spec.maxReplicas} {.spec.metrics}{"\n"}{end}'
kubectl get scaledobjects.keda.sh -n <namespace> 2>&1 | head -5
```

A KEDA error that the resource type does not exist means KEDA is not installed; say so. If an HPA targets the workload on CPU or memory **utilization**, that utilization is usage divided by the request. A lower request raises the reading, and the HPA adds replicas. Show it with the HPA's own numbers: `desired = ceil(current_replicas × (usage / new_request) / target)`. For example, 4 replicas using 300m each against a 70% target: at a 500m request, `ceil(4 × 0.60 / 0.70) = 4`; at 350m, `ceil(4 × 0.86 / 0.70) = 5`. More replicas reserve more in total, so the cut can free nothing. Where the pods also cannot be placed (Pending replicas, a capped pool), the cut makes the backlog larger. The lever is usually the HPA target or `minReplicas`, not the request.

## 2. QoS class, and requests against limits

```bash
kubectl get pods -n <namespace> -l <selector> -o jsonpath='{range .items[*]}{.metadata.name} {.status.qosClass}{"\n"}{end}'
kubectl get deploy <name> -n <namespace> -o jsonpath='{range .spec.template.spec.containers[*]}{.name} req={.resources.requests} lim={.resources.limits}{"\n"}{end}'
kubectl top pods -n <namespace> -l <selector> --containers
```

`Guaranteed` means requests equal limits on every container, on both dimensions. To keep it `Guaranteed`, a lower request is also a lower limit: a CPU limit throttles the container at that value, and a memory limit OOM-kills it there. Compare usage with the **limit**, not only the request. A container whose CPU use already sits at its limit is throttled now, and a lower figure throttles it harder. Lowering the request alone moves the pod to `Burstable`, so say that too. For `Burstable`, a lower memory request moves the pod earlier in the kubelet's eviction order under memory pressure. One `kubectl top` reading is a snapshot: name it as one, and do not size a limit from it. A proposed figure needs a peak over a longer window, such as a week of Cloud Monitoring history. If you have none, say that the proposal rests on one snapshot and must be checked against a longer peak before anyone applies it.

## 3. Other objects that share its pods or its volumes

```bash
kubectl get svc,pdb -n <namespace> -o jsonpath='{range .items[*]}{.kind} {.metadata.name} {.spec.selector}{"\n"}{end}'
kubectl get deploy <name> -n <namespace> -o jsonpath='{.spec.template.spec.volumes[*].persistentVolumeClaim.claimName}'
kubectl get pods -n <namespace> -o jsonpath='{range .items[*]}{.metadata.name} {.spec.volumes[*].persistentVolumeClaim.claimName}{"\n"}{end}'
```

Report each Service and PodDisruptionBudget whose selector matches the workload's pod labels, and each other pod that mounts the same claim. A PDB with `minAvailable` equal to the replica count blocks every drain, and a change that alters the replica count changes what the budget allows. Two controllers whose selectors overlap mean a change to one also changes what the other's Service sends traffic to.

## 4. Files shared across clusters

You read one cluster and hold no repository. Report where the workload says it comes from, so the Platform Agent can check the file:

```bash
kubectl get deploy <name> -n <namespace> -o jsonpath='{.metadata.labels}{"\n"}{.metadata.annotations}'
```

Name the source the metadata shows:

- Helm: the `meta.helm.sh/release-name` and `meta.helm.sh/release-namespace` annotations.
- Argo CD: the `argocd.argoproj.io/tracking-id` annotation, or the `argocd.argoproj.io/instance` or `app.kubernetes.io/instance` label.
- Flux: the `kustomize.toolkit.fluxcd.io/name` or `helm.toolkit.fluxcd.io/name` label.
- Config Sync: any `configsync.gke.io/` annotation; `configmanagement.gke.io/source-path` names the file.

Then say that one values file or kustomize base can render this workload on other clusters, so an edit there changes all of them, and that the Platform Agent must confirm which file declares it before a change is written. If the metadata shows no source, say that.

## 5. Per-node packing, with both vCPU and memory

Needed for any claim about nodes, machine types or "fits". Read the pool's nodes and what is already booked on them:

```bash
kubectl get nodes -l cloud.google.com/gke-nodepool=<pool> -o jsonpath='{range .items[*]}{.metadata.name} {.metadata.labels.node\.kubernetes\.io/instance-type} cpu={.status.allocatable.cpu} mem={.status.allocatable.memory}{"\n"}{end}'
kubectl describe nodes -l cloud.google.com/gke-nodepool=<pool> | grep -A6 'Allocated resources'
```

Calculate per dimension and show both: `nodes_needed = max(ceil(total_cpu_requests / allocatable_cpu_per_node), ceil(total_memory_requests / allocatable_memory_per_node))`, with DaemonSet requests counted on every node. Use allocatable, not the machine's nominal size: on a small node, system reservations take a large share. For a machine-type change, compare both dimensions: `e2-highcpu-8` (8 vCPU, 8 GB) to `e2-standard-4` (4 vCPU, 16 GB) halves the CPU and doubles the memory, so "halves the resources" is wrong, and the node count follows from the packing, not from the vCPU ratio. Say which dimension binds.

## Writing the answer

Pass `rightsizing` to `kanban_complete`, one entry per workload or pool:

- `workload`: its name.
- `dependency_kind`: `hpa`, `guaranteed_qos`, `pdb_or_pvc`, `shared_file` or `none`, from checks 1 to 4; `not_on_cluster` when your cluster does not run it. If several apply, pick the first in that order and name the others in `dependency`.
- `dependency`: one sentence on what the dependency means for a cut, for example "a lower request adds replicas" or "a lower request is also a lower limit".
- `checks`: one finding each for `hpa`, `qos`, `sharing`, `source` and `packing`, with the command or reading behind it. "No HPA targets it" is a finding. Not needed for `not_on_cluster`.

Each field is one line of at most 200 characters, with no code fence. The tool renders these entries in your `result`, and the Platform Agent's summary gets the same lines, so the person sees each dependency whatever the summary says. `kanban_complete` refuses, once, a completion without them on a card that names this skill.

In `result` itself, after at most one bold sentence, for each workload or pool:

1. **The proposal:** the new figures, or why no cut is safe. A dependency that makes the cut unsafe is the answer, stated first.
2. **The arithmetic:** each effect claim with its inputs and its result, in resource units, never in currency.

Hand the change back to the Platform Agent as a proposal. You do not apply it.
