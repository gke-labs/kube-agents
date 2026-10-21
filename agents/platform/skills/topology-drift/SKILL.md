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

## Step 1: Run `leeway report`

To collect live topology distribution evidence against a target cluster from the sandbox, pin a per-target `KUBECONFIG` under `$HERMES_HOME/.kubeconfigs/` per `AGENTS.md` (exactly as in `gke-basics`), then run `leeway report --topology-tier-c-signals` (`leeway report` always outputs JSON (`leeway.report/v1alpha1`), and `--topology-tier-c-signals` includes unconstrained Tier C workloads alongside Tier A/B findings):

```bash
PROJECT="$GKE_PROJECT_ID"   # CLUSTER and LOCATION come from the request
export KUBECONFIG="${HERMES_HOME:-/opt/data}/.kubeconfigs/kubeconfig_${PROJECT}_${CLUSTER}_${LOCATION}.yaml"
gcloud container clusters get-credentials "$CLUSTER" --location="$LOCATION" --project="$PROJECT" --quiet
```

Once `KUBECONFIG` points at the target cluster, run `leeway report` (or pass `--clusters <project>/<location>/<cluster>` directly, since `--clusters` takes `<project>/<location>/<cluster>` triples rather than kubeconfig context names):

```bash
# Scan a specific namespace on the target cluster:
leeway report --topology-tier-c-signals --namespace <namespace>

# Scan all namespaces on the target cluster:
leeway report --topology-tier-c-signals

# Or target a GKE cluster triple directly (invokes gcloud get-credentials under the hood):
leeway report --topology-tier-c-signals --clusters "${PROJECT}/${LOCATION}/${CLUSTER}" --namespace <namespace>
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

### Reading `leeway report` (`leeway.report/v1alpha1`) Output

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
    - `maxDomainShare`: fraction of running pods concentrated in a single domain (`maxDomainShare: 1.0` means `100%` of running replicas fail if that single zone goes down)
  - `domains[]`: per-zone breakdown with `domain`, `actual` (running pods), `expected`, `delta`, `readyNodes`, and `note`
  - `intent`: `{source, confidence, weighting, maxSkew, evidence[]}` extracted from `topologySpreadConstraints`, pod anti-affinity, or cluster defaults
  - `suspectedCause` (`schedule_anyway_soft`, `affinity_conflict`, `taint_blocked`, `resource_starvation`, `volume_topology_locked`, `node_count_asymmetry`, `daemonset_bound`, `unknown`) and `contributingFactors[]`
  - `remediation`: `{kind, summary, yaml}` distinguishing `manifest_patch` from `storage_migration` and `capacity_action`

## Step 2: Attribute the Root Cause

For every skewed workload (`score.observedSkew > 1` or `score.maxDomainShare >= 0.8` on a multi-zone cluster), classify its cause using the decision matrix below before proposing any remediation:

| Cause Category                                                    | Signature in `leeway report` & Kubernetes                                                                                                                                                                                                 | Confirming Read-Only Checks                                                                                                                                                                                                                                                                                                                           | Correct Remediation                                                                                                                                                                                                                                         |
| :---------------------------------------------------------------- | :---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **1. Scheduling Misconfiguration** (e.g., `zone-pinned-api`)      | All replicas are `Running` in one zone (`score.maxDomainShare == 1.0`, zero `Pending` pods). `suspectedCause` is `schedule_anyway_soft` or `affinity_conflict` (`whenUnsatisfiable: ScheduleAnyway` and/or single-zone `nodeAffinity`).   | Inspect the workload's `spec.template.spec.topologySpreadConstraints` and `spec.template.spec.affinity.nodeAffinity`. Verify that no PVCs or `Pending` pods are involved.                                                                                                                                                                             | **Manifest edit:** remove or widen the single-zone `nodeAffinity` / `nodeSelector` to cover all cluster zones, and set `whenUnsatisfiable: DoNotSchedule` if strict zonal spread is required.                                                               |
| **2. Zone-Pinned Storage (Volume)** (e.g., `zone-bound-store`)    | Workload is a `StatefulSet` (or mounts RWO PVCs) with `suspectedCause: "volume_topology_locked"` (or `volumeClaimTemplates` referencing a zonal `StorageClass` such as `seeded-zonal-pd`), zero `Pending` pods, and all pods in one zone. | Check the `PersistentVolumeClaim` and `StorageClass` (`kubectl get pvc -n <namespace>` and `kubectl get storageclass <storage_class_name> -o yaml`). Look for `allowedTopologies` pinning `topology.gke.io/zone` to one zone or zonal `pd.csi.storage.gke.io` disks bound in a single zone.                                                           | **Storage migration:** provision a regional `StorageClass` or migrate volume state across zones. Warn that editing `topologySpreadConstraints` to `DoNotSchedule` without migrating the zonal PVCs will break scheduling (`VolumeNodeAffinityConflict`).    |
| **3. Node Capacity Starvation** (e.g., `capacity-starved-worker`) | `suspectedCause: "resource_starvation"` (or `Pending` pods coexist with `Running` pods concentrated in one zone with `score.maxDomainShare == 1.0`).                                                                                      | Inspect the `Pending` pods and node allocatable capacity (`kubectl get pods -n <namespace> -o wide` and `kubectl describe pod <pending_pod_name> -n <namespace>`). Confirm `FailedScheduling` events citing `Insufficient cpu` or `Insufficient memory` in the starved zone (often caused by node size asymmetry or competing zone-pinned workloads). | **Capacity or sizing change:** add node capacity in the starved zone, enable autoscaling, rebalance competing workloads in the full zone, or right-size container CPU/memory requests. Do **not** blame or edit the workload's `topologySpreadConstraints`. |

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
