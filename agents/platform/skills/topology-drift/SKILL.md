---
name: topology-drift
description: Detect and attribute pod topology distribution skew and single-zone blast-radius exposure across GKE clusters and namespaces using leeway report, distinguishing scheduling misconfiguration (ScheduleAnyway or single-zone affinity), zone-pinned storage (PersistentVolumeClaims on zonal StorageClasses), and node capacity starvation (Pending pods on Insufficient cpu or memory).
---

# Topology Drift & Zonal Skew Analysis

Use this skill to detect, quantify, and attribute pod topology distribution anomalies across availability zones (`topology.kubernetes.io/zone`) and nodes (`kubernetes.io/hostname`) in GKE clusters.

Noticing that replicas sit in one zone is only the first half of the diagnosis. An accurate topology analysis must **attribute the root cause** before recommending a fix:

1. **Scheduling misconfiguration** — `topologySpreadConstraints` configured with `whenUnsatisfiable: ScheduleAnyway` (a soft preference mistaken for hard protection) or a single-zone `nodeAffinity` / `nodeSelector` pinning pods to one zone. Fix: declarative manifest edit.
2. **Zone-pinned volume (`PersistentVolumeClaim`)** — a `StatefulSet` or stateful workload bound to zonal persistent disks (for example, a `StorageClass` whose `allowedTopologies` pins volumes to one zone). Fix: storage migration or regional `StorageClass`, **never** a pod-template spread edit alone.
3. **Node capacity starvation** — a workload whose running pods sit in one zone while its remaining replicas stay `Pending` with `FailedScheduling` (`Insufficient cpu` or `Insufficient memory`) because nodes in the other zone are full. Fix: node-pool capacity or request right-sizing, **never** a topology spread constraint edit.

## When to Use

- A user asks which workloads have uneven zonal distribution, topology drift, or single-zone blast-radius risk (`100%` of running replicas in a single availability zone).
- Before or after a cluster upgrade, node-pool resize, or incident to verify that multi-replica workloads (`replicas > 1`) are spread across zones.
- Triaging a workload whose replicas are all in one zone or partially `Pending` across a multi-zonal cluster.

## Step 1: Check Cached Watch Reports or Run `leeway report`

If a recent daily topology watch report exists on disk at `/opt/data/leeway/reports/<cluster>/latest.json`, read it first to inspect the latest fleet snapshot.

To collect live topology distribution evidence against one or more clusters, authenticate kubeconfig for the target cluster(s) and run `leeway report`:

```bash
gcloud container clusters get-credentials <cluster_name> --location <cluster_location> --project <project_id>

# Scan a specific namespace in the current kubeconfig context:
leeway report --namespace <namespace> --format json

# Scan all non-system namespaces in the current kubeconfig context:
leeway report --format json

# Scan multiple kubeconfig contexts in parallel:
leeway report --clusters <context_a>,<context_b> --format json
```

### Reading `leeway report --format json` Output

Each `clusters[]` entry in the JSON report contains:
- `zoneCount` and `nodeCount`: the cluster's available failure domains and nodes (`nodes[]` maps each node to its `zone`). A single-zone cluster (`zoneCount == 1`) cannot spread pods across zones by construction.
- `zoneSkewScore` and `hostSkewScore`: cluster-wide health scores (`0`–`100`, where `100` is balanced).
- `workloads[]`: per-workload distribution records for controllers with `replicas > 1`:
  - `kind`, `namespace`, `name`, `replicas`, `readyReplicas`, `runningPods`, `pendingPods`
  - `zoneCounts`: running pod count per `topology.kubernetes.io/zone`
  - `zoneSkew`: difference between the most-populated and least-populated cluster zone (including `0` for cluster zones where the workload has no running pods)
  - `zoneShares` and `maxZoneShare`: fraction of running pods in each zone (`maxZoneShare: 1.0` means `100%` of running replicas fail if that single zone goes down)
  - `constraints`: extracted scheduling intent from the pod template:
    - `topologySpreadConstraints[]`: `topologyKey`, `maxSkew`, and `whenUnsatisfiable` (`DoNotSchedule` vs `ScheduleAnyway`)
    - `hasNodeSelector`, `nodeSelector`
    - `hasNodeAffinity`, `requiredZoneAffinityValues`, `preferredZoneAffinityValues`
    - `hasPodAntiAffinity`, `requiredZoneAntiAffinity`, `preferredZoneAntiAffinity`
    - `volumeClaimStorageClasses`: `StorageClass` names referenced by `volumeClaimTemplates` or PVC volumes

## Step 2: Attribute the Root Cause

For every skewed workload (`zoneSkew > 1` or `maxZoneShare >= 0.8` on a cluster with `zoneCount >= 2`), classify its cause using the decision matrix below before proposing any remediation:

| Cause Category | Signature in `leeway report` & Kubernetes | Confirming Read-Only Checks | Correct Remediation |
| :--- | :--- | :--- | :--- |
| **1. Scheduling Misconfiguration** | `pendingPods == 0`, `runningPods == replicas`, `maxZoneShare == 1.0`. `constraints` shows `whenUnsatisfiable: "ScheduleAnyway"` on `topology.kubernetes.io/zone` and/or `requiredZoneAffinityValues` pinning a single zone. | Inspect the workload's `spec.template.spec.topologySpreadConstraints` and `spec.template.spec.affinity.nodeAffinity`. Verify that no PVCs or `Pending` pods are involved. | **Manifest edit:** remove or widen the single-zone `nodeAffinity` / `nodeSelector` to cover all cluster zones, and set `whenUnsatisfiable: DoNotSchedule` if strict zonal spread is required. |
| **2. Zone-Pinned Storage (Volume)** | Workload is a `StatefulSet` (or mounts RWO PVCs) with `volumeClaimStorageClasses` populated, `pendingPods == 0`, and all pods running in one zone. | Check the `PersistentVolumeClaim` and `StorageClass` (`kubectl get pvc -n <namespace>` and `kubectl get storageclass <storage_class_name> -o yaml`). Look for `allowedTopologies` pinning `topology.gke.io/zone` to one zone or zonal `pd.csi.storage.gke.io` disks bound in a single zone. | **Storage migration:** provision a regional `StorageClass` or migrate volume state across zones. Warn that editing `topologySpreadConstraints` to `DoNotSchedule` without migrating the zonal PVCs will break scheduling (`VolumeNodeAffinityConflict`). |
| **3. Node Capacity Starvation** | `pendingPods > 0` and `runningPods >= 1`, with all running pods in one zone (`maxZoneShare == 1.0`). `topologySpreadConstraints` may already be present (`ScheduleAnyway`). | Inspect the `Pending` pods and node allocatable capacity (`kubectl get pods -n <namespace> -o wide` and `kubectl describe pod <pending_pod_name> -n <namespace>`). Confirm `FailedScheduling` events citing `Insufficient cpu` or `Insufficient memory` in the starved zone (often caused by node size asymmetry or competing zone-pinned workloads). | **Capacity or sizing change:** add node capacity in the starved zone, enable autoscaling, rebalance competing workloads in the full zone, or right-size container CPU/memory requests. Do **not** blame or edit the workload's `topologySpreadConstraints`. |

### Confirming Commands

Run targeted read-only `kubectl` commands to verify the attribution for skewed workloads:

```bash
# Inspect pod placement and Pending status in the namespace:
kubectl get pods -n <namespace> -o wide

# For workloads with Pending pods, read scheduler events to confirm Insufficient cpu/memory:
kubectl describe pod <pending_pod_name> -n <namespace>

# For StatefulSets or PVC-backed workloads, inspect PVC binding and StorageClass topology rules:
kubectl get pvc -n <namespace>
kubectl get storageclass <storage_class_name> -o yaml
```

## Step 3: Report Blast Radius & Remediate Safely

In your report, always state for each skewed workload:
1. **Workload identity:** `kind/name` and `namespace` (plus cluster name in multi-cluster checks).
2. **Blast radius:** per-zone running pod breakdown (e.g., `2/2` running replicas in `<zone_a>`, `0` in `<zone_b>` — `100%` single-zone blast radius if `<zone_a>` fails; plus any `Pending` replica count).
3. **Attributed root cause:** explicitly distinguish scheduling configuration (`ScheduleAnyway` / single-zone `nodeAffinity`), zone-pinned storage (`PersistentVolumeClaim` / `StorageClass` `allowedTopologies`), or node capacity starvation (`Pending` pods with `Insufficient cpu` or `Insufficient memory`).
4. **Tailored next step:** recommend a manifest change only for scheduling misconfiguration, a storage migration for volume-pinned workloads, and a capacity or resource-sizing action for capacity-starved workloads. When asked to propose a GitOps fix for a scheduling misconfiguration, use the `submit-suggestion` skill. Never mutate live cluster resources directly.
