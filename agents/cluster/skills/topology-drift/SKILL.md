---
name: topology-drift
description: Detect and attribute pod topology distribution skew and single-zone blast-radius exposure across GKE clusters and namespaces using leeway report, distinguishing scheduling misconfiguration (ScheduleAnyway or single-zone affinity), zone-pinned storage (PersistentVolumeClaims on zonal StorageClasses), and node capacity starvation (Pending pods on Insufficient cpu or memory).
---

# Topology Drift & Zonal Skew Analysis (Cluster Agent)

Use this skill to detect, quantify, and attribute pod topology distribution anomalies across availability zones (`topology.kubernetes.io/zone`) and nodes (`kubernetes.io/hostname`) on your assigned GKE cluster.

Noticing that replicas sit in one zone is only the first half of the diagnosis. An accurate topology analysis must **attribute the root cause** before recommending a fix:

1. **Scheduling misconfiguration** — `topologySpreadConstraints` configured with `whenUnsatisfiable: ScheduleAnyway` (a soft preference mistaken for hard protection) or a single-zone `nodeAffinity` / `nodeSelector` pinning pods to one zone. Fix: declarative manifest edit.
2. **Zone-pinned volume (`PersistentVolumeClaim`)** — a `StatefulSet` or stateful workload bound to zonal persistent disks (for example, a `StorageClass` whose `allowedTopologies` pins volumes to one zone). Fix: storage migration or regional `StorageClass`, **never** a pod-template spread edit alone.
3. **Node capacity starvation** — a workload whose running pods sit in one zone while its remaining replicas stay `Pending` with `FailedScheduling` (`Insufficient cpu` or `Insufficient memory`) because nodes in the other zone are full. Fix: node-pool capacity or request right-sizing, **never** a topology spread constraint edit.

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
- `domains["topology.kubernetes.io/zone"]`: map of cluster availability zones to `{nodes, ready, usable, allocatable_cpu_milli, allocatable_mem_bytes}`.
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

For every skewed workload (`score.observedSkew > 1` or `score.maxDomainShare >= 0.8` on a multi-zone cluster), classify the root cause using `leeway report` and confirming `kubectl` reads:

1. **Scheduling Misconfiguration (e.g., `zone-pinned-api`):**
   - All replicas are `Running` in a single zone (`score.maxDomainShare == 1.0`, zero `Pending` pods).
   - `suspectedCause` is `schedule_anyway_soft` or `affinity_conflict`, or the pod spec shows `whenUnsatisfiable: ScheduleAnyway` alongside a single-zone `nodeAffinity` / `nodeSelector`.
   - Recommend removing or widening the single-zone `nodeAffinity` / `nodeSelector` and tightening `whenUnsatisfiable` to `DoNotSchedule` when strict spread is required.
2. **Zone-Pinned Storage (Volume) (e.g., `zone-bound-store`):**
   - `StatefulSet` or PVC-backed workload with `suspectedCause: "volume_topology_locked"` (or `volumeClaimTemplates` referencing a zonal `StorageClass` such as `seeded-zonal-pd`), zero `Pending` pods, and all pods in one zone.
   - Confirm with `kubectl get pvc -n <namespace>` and `kubectl get storageclass <storage_class_name> -o yaml` whether `allowedTopologies` or zonal persistent disks bind the replicas to one zone.
   - Recommend storage migration or a regional `StorageClass` (not a pod-template spread constraint edit).
3. **Node Capacity Starvation (e.g., `capacity-starved-worker`):**
   - `suspectedCause: "resource_starvation"` (or `Pending` pods coexist with `Running` pods concentrated in the zone that still has headroom).
   - Confirm with `kubectl describe pod <pending_pod_name> -n <namespace>` that `Pending` pods fail scheduling on `Insufficient cpu` or `Insufficient memory` in the starved zone.
   - Recommend adding capacity in the starved zone, rebalancing competing workloads, or right-sizing CPU/memory requests (not editing `topologySpreadConstraints`).

```bash
kubectl get pods -n <namespace> -o wide
kubectl describe pod <pending_pod_name> -n <namespace>
kubectl get pvc -n <namespace>
kubectl get storageclass <storage_class_name> -o yaml
```

## Step 3: Recording the Finding

You are a Cluster Agent under a strict read-only boundary: never mutate cluster state directly. Complete your kanban card with the per-workload blast radius, zone counts, attributed root cause, and evidence (following the `gke-workload-troubleshooting` skill's `kanban_complete` structure unless your card specifies an explicit output format).
