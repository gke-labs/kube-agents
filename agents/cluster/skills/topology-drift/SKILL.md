---
name: topology-drift
description: Detect and attribute pod topology distribution skew and single-zone blast-radius exposure across GKE clusters using leeway report, distinguishing scheduling misconfiguration (ScheduleAnyway or single-zone affinity), missing topologySpreadConstraints, zone-pinned storage (PersistentVolumeClaims on zonal StorageClasses), node capacity starvation (Pending pods on Insufficient cpu or memory), and single-zone node pools. For single-cluster requests on a managed cluster, delegate runtime debugging to that cluster's Cluster Agent per AGENTS.md.
---

# Topology Drift & Zonal Skew Analysis (Cluster Agent)

Use this skill to detect, quantify, and attribute pod topology distribution anomalies across availability zones (`topology.kubernetes.io/zone`) and nodes (`kubernetes.io/hostname`) on your assigned GKE cluster.

Noticing that replicas sit in one zone is only the first half of the diagnosis. An accurate topology analysis must **attribute the root cause** before recommending a fix:

1. **Scheduling misconfiguration** — `topologySpreadConstraints` configured with `whenUnsatisfiable: ScheduleAnyway` (a soft preference mistaken for hard protection) or a single-zone `nodeAffinity` / `nodeSelector` pinning pods to one zone while other zones have ready nodes in the target pool. Fix: declarative manifest edit.
2. **Missing `topologySpreadConstraints`** — a multi-replica workload with no explicit zonal spread constraint (`intent.source` is `cluster_default` or `none`). Fix: declarative manifest edit adding `topologySpreadConstraints` across `topology.kubernetes.io/zone` (after verifying multi-zone node capacity and no zonal PVCs).
3. **Zone-pinned volume (`PersistentVolumeClaim`)** — a `StatefulSet` or stateful workload bound to zonal persistent disks (for example, a `StorageClass` whose `allowedTopologies` pins volumes to one zone). Fix: storage migration or regional `StorageClass`, **never** a pod-template spread edit alone.
4. **Node capacity starvation** — a workload whose running pods sit in one zone while its remaining replicas stay `Pending` with `FailedScheduling` (`Insufficient cpu` or `Insufficient memory`) because nodes in the other zone are full. Fix: node-pool capacity or request right-sizing, **never** a topology spread constraint edit.
5. **Single-zone node pool / node-count asymmetry** — all nodes in the cluster or in the target node pool sit in a single zone (`suspectedCause: "node_count_asymmetry"` or `len(domains["topology.kubernetes.io/zone"]) == 1`). Fix: node-pool / cluster capacity action (add zones or multi-zone node pools), **never** widening `nodeAffinity` or tightening `DoNotSchedule` onto zones with no matching nodes.
6. **Other / unknown cause** — `taint_blocked`, `daemonset_bound`, or `unknown`. Fix: withhold manifest spread edits until node taints, DaemonSet controller semantics, or custom scheduler predicates are inspected.

## Step 1: Run `leeway report`

Your `KUBECONFIG` is pre-pinned to your assigned cluster at profile creation (`AGENTS.md`). Run `leeway report --topology-tier-c-signals` directly against that kubeconfig (`leeway report` always outputs JSON (`leeway.report/v1alpha1`), and `--topology-tier-c-signals` includes unconstrained Tier C workloads alongside Tier A/B findings):

```bash
# Scan a specific namespace:
leeway report --topology-tier-c-signals --namespace <namespace>

# Scan all namespaces on the cluster:
leeway report --topology-tier-c-signals
```

If `leeway` is not installed in the current sandbox image (`! command -v leeway >/dev/null 2>&1`), collect the equivalent node-zone, pod-placement, controller-constraint, PVC, and scheduler-event evidence directly with read-only `kubectl` commands:

```bash
kubectl get nodes -L topology.kubernetes.io/zone
kubectl get pods -n <namespace> -o wide
kubectl get deploy,statefulset -n <namespace> -o yaml
kubectl get pvc -n <namespace>
kubectl get storageclass -o yaml
kubectl get events -n <namespace> --field-selector reason=FailedScheduling
```

### Key Fields in `leeway report` (`leeway.report/v1alpha1`)

Each `clusters[]` entry in the JSON report contains:

- `cluster`, `source` (`gke:<project>/<location>/<cluster>`), and `status` (`ok` or `error`).
- `domains["topology.kubernetes.io/zone"]`: map of cluster availability zones to `{nodes, ready, usable, allocatable_cpu_milli, allocatable_mem_bytes}`. A single-zone cluster (`len(domains["topology.kubernetes.io/zone"]) == 1`) cannot spread pods across zones by construction.
- `topology_findings[]`: per-workload placement and skew findings (`subjects[]` is also populated when `--all` or `--min-drift` is passed):
  - `kind` (`leeway.contract_violated`, `leeway.placement_drift`, `leeway.baseline_breach`, `leeway.domain_unavailable`), `tier` (`A`, `B`, `C`), and `severity` (`critical`, `warning`, `info`)
  - `subject`: `{kind, namespace, name, replicas}`
  - `topologyKey`: e.g. `topology.kubernetes.io/zone` or `kubernetes.io/hostname`
  - `score`:
    - `drift`: normalized placement drift score (`0.0`–`1.0`)
    - `observedSkew`, `minAchievableSkew`, `excessSkew`, `relocationDistance`
    - `maxDomainShare`: fraction of running pods concentrated in a single domain (`maxDomainShare: 1.0` means `100%` of running replicas are lost if that single zone fails)
  - `domains[]`: per-zone breakdown with `domain`, `actual` (running pods), `expected`, `delta`, `readyNodes`, and `note`
  - `intent`: `{source, confidence, weighting, maxSkew, evidence[]}` extracted from `topologySpreadConstraints`, pod anti-affinity, or cluster defaults
  - `suspectedCause` (`schedule_anyway_soft`, `affinity_conflict`, `taint_blocked`, `resource_starvation`, `volume_topology_locked`, `node_count_asymmetry`, `daemonset_bound`, `unknown`) and `contributingFactors[]`
  - `remediation`: `{kind, summary, yaml}` distinguishing `manifest_patch` from `storage_migration` and `capacity_action`

## Step 2: Attribute the Root Cause

For every skewed workload (`score.observedSkew > 1` or `score.maxDomainShare >= 0.8` on a multi-zone cluster), classify its cause using the decision matrix below before proposing any remediation:

| Cause Category                                                                                                          | Signature in `leeway report` & Kubernetes                                                                                                                                                                                                                                                         | Confirming Read-Only Checks                                                                                                                                                                                                                                                             | Correct Remediation                                                                                                                                                                                                                                                                         |
| :---------------------------------------------------------------------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | :-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **1. Scheduling Misconfiguration** (e.g., `Deployment/checkout-api` with `ScheduleAnyway` + single-zone `nodeAffinity`) | All replicas are `Running` in one zone (`score.maxDomainShare == 1.0`, zero `Pending` pods). `suspectedCause` is `schedule_anyway_soft` or `affinity_conflict` (`whenUnsatisfiable: ScheduleAnyway` and/or single-zone `nodeAffinity`), while ready nodes matching the pool exist in other zones. | Inspect `spec.template.spec.topologySpreadConstraints` and `spec.template.spec.affinity.nodeAffinity` / `nodeSelector`. Verify that other zones have ready nodes in the target pool and that no PVCs or `Pending` pods are involved.                                                    | **Manifest edit:** remove or widen the single-zone `nodeAffinity` / `nodeSelector` to cover all cluster zones, and set `whenUnsatisfiable: DoNotSchedule` if strict zonal spread is required.                                                                                               |
| **2. Missing `topologySpreadConstraints`** (e.g., unconstrained `Deployment/frontend-web`)                              | All or most replicas are `Running` in one zone with zero `Pending` pods, `intent.source` is `"cluster_default"` or `"none"`, and the pod template declares no `topologySpreadConstraints` across `topology.kubernetes.io/zone`.                                                                   | Inspect `spec.template.spec.topologySpreadConstraints`. Confirm multiple zones have ready nodes with headroom and the workload mounts no zonal RWO PVCs.                                                                                                                                | **Manifest edit:** add a `topologySpreadConstraints` entry for `topology.kubernetes.io/zone` (`maxSkew: 1`) matching the workload's pod selector labels.                                                                                                                                    |
| **3. Zone-Pinned Storage (Volume)** (e.g., `StatefulSet/orders-db` on a zonal `StorageClass`)                           | Workload is a `StatefulSet` (or mounts RWO PVCs) with `suspectedCause: "volume_topology_locked"` (or `volumeClaimTemplates` referencing a zonal `StorageClass`), zero `Pending` pods, and all pods in one zone.                                                                                   | Check `PersistentVolumeClaim` and `StorageClass` (`kubectl get pvc -n <namespace>` and `kubectl get storageclass <storage_class_name> -o yaml`). Look for `allowedTopologies` pinning `topology.gke.io/zone` to one zone or zonal `pd.csi.storage.gke.io` disks bound in a single zone. | **Storage migration:** provision a regional `StorageClass` or migrate volume state across zones. Warn that editing `topologySpreadConstraints` to `DoNotSchedule` without migrating the zonal PVCs will break scheduling (`VolumeNodeAffinityConflict`).                                    |
| **4. Node Capacity Starvation** (e.g., `Deployment/batch-worker` with `Pending` pods on `Insufficient cpu`)             | `suspectedCause: "resource_starvation"` (or `Pending` pods coexist with `Running` pods concentrated in one zone with `score.maxDomainShare == 1.0`).                                                                                                                                              | Inspect the `Pending` pods and node allocatable capacity (`kubectl get pods -n <namespace> -o wide` and `kubectl describe pod <pending_pod_name> -n <namespace>`). Confirm `FailedScheduling` events citing `Insufficient cpu` or `Insufficient memory` in the starved zone.            | **Capacity or sizing change:** add node capacity in the starved zone, enable autoscaling, rebalance competing workloads in the full zone, or right-size container CPU/memory requests. Do **not** blame or edit the workload's `topologySpreadConstraints`.                                 |
| **5. Single-Zone Node Pool / Node-Count Asymmetry** (e.g., workload on a pool provisioned in one zone only)             | `suspectedCause: "node_count_asymmetry"`, `len(domains["topology.kubernetes.io/zone"]) == 1`, or the node pool targeted by the workload's `nodeSelector` / `nodeAffinity` has nodes in only one zone.                                                                                             | Check node zones per pool (`kubectl get nodes -L topology.kubernetes.io/zone,cloud.google.com/gke-nodepool`). Confirm no other zone has nodes in that pool.                                                                                                                             | **Capacity / node-pool change:** expand the node pool's `nodeLocations` across multiple zones (or provision a multi-zone node pool) via IaC/Config Connector. Never widen `nodeAffinity` or set `DoNotSchedule` onto zones that have no nodes in the pool, or moved pods will go `Pending`. |
| **6. Other / Unknown Cause** (`taint_blocked`, `daemonset_bound`, `unknown`)                                            | `suspectedCause` is `taint_blocked`, `daemonset_bound`, or `unknown`, or none of rows 1–5 matches the live evidence.                                                                                                                                                                              | Inspect node taints (`kubectl get nodes -o custom-columns=NAME:.metadata.name,TAINTS:.spec.taints`), workload tolerations, and controller type.                                                                                                                                         | **Withhold manifest spread edits:** report the blocking taint, controller constraint, or unverified cause and do not propose a `topologySpreadConstraints` patch until the underlying constraint is resolved.                                                                               |

```bash
kubectl get pods -n <namespace> -o wide
kubectl describe pod <pending_pod_name> -n <namespace>
kubectl get pvc -n <namespace>
kubectl get storageclass <storage_class_name> -o yaml
```

## Step 3: Recording the Finding

You are a Cluster Agent under a strict read-only boundary: never mutate cluster state directly. Complete your kanban card with the per-workload blast radius, zone counts, attributed root cause from the 6-row matrix above, and evidence (following the `gke-workload-troubleshooting` skill's `kanban_complete` structure unless your card specifies an explicit output format).
