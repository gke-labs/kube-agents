---
name: topology-drift
description: Detect and attribute pod topology distribution skew and single-zone blast-radius exposure across GKE clusters using leeway report, distinguishing scheduling misconfiguration (ScheduleAnyway or single-zone affinity), missing topologySpreadConstraints, zone-pinned storage (PersistentVolumeClaims on zonal StorageClasses), node capacity starvation (Pending pods on Insufficient cpu or memory), and single-zone node pools. For single-cluster requests on a managed cluster, delegate runtime debugging to that cluster's Cluster Agent per AGENTS.md.
---

# Topology Drift & Zonal Skew Analysis

Use this skill to detect, quantify, and attribute pod topology distribution anomalies across availability zones (`topology.kubernetes.io/zone`) and nodes (`kubernetes.io/hostname`) across GKE clusters.

Noticing that replicas sit in one zone is only the first half of the diagnosis. An accurate topology analysis must **attribute the root cause** before recommending a fix:

1. **Scheduling misconfiguration** — `topologySpreadConstraints` configured with `whenUnsatisfiable: ScheduleAnyway` (a soft preference mistaken for hard protection) or a single-zone `nodeAffinity` / `nodeSelector` pinning pods to one zone while other zones have ready nodes in the target pool. Fix: declarative manifest edit.
2. **Missing `topologySpreadConstraints`** — a multi-replica workload with no explicit zonal spread constraint (`intent.source` is `cluster_default` or `none`). Fix: declarative manifest edit adding `topologySpreadConstraints` across `topology.kubernetes.io/zone` (after verifying multi-zone node capacity and no zonal PVCs).
3. **Zone-pinned volume (`PersistentVolumeClaim`)** — a `StatefulSet` or stateful workload bound to zonal persistent disks (for example, a `StorageClass` whose `allowedTopologies` pins volumes to one zone). Fix: storage migration or regional `StorageClass`, **never** a pod-template spread edit alone.
4. **Node capacity starvation** — a workload whose running pods sit in one zone while its remaining replicas stay `Pending` with `FailedScheduling` (`Insufficient cpu` or `Insufficient memory`) because nodes in the other zone are full. Fix: node-pool capacity or request right-sizing, **never** a topology spread constraint edit.
5. **Single-zone node pool / node-count asymmetry** — all nodes in the cluster or in the target node pool sit in a single zone (`suspectedCause: "node_count_asymmetry"` or `len(domains["topology.kubernetes.io/zone"]) == 1`). Fix: node-pool / cluster capacity action (add zones or multi-zone node pools), **never** widening `nodeAffinity` or tightening `DoNotSchedule` onto zones with no matching nodes.
6. **Other / unknown cause** — `taint_blocked`, `daemonset_bound`, or `unknown`. Fix: withhold manifest spread edits until node taints, DaemonSet controller semantics, or custom scheduler predicates are inspected.

## Delegation vs. Direct Execution (`AGENTS.md`)

Per `AGENTS.md` ("Delegation") and `SOUL.md` §6:

- **Single-cluster request on a managed cluster:** First call `get_cluster_profile_name(project, cluster, location)`. When `exists` is `true`, **delegate** the single-cluster topology diagnosis to that cluster's Cluster Agent via `kanban_create(assignee="<profile-name>", ...)` (which runs its own `topology-drift` skill) rather than debugging the single cluster inline on the Platform Agent. When the Cluster Agent card completes, read its RCA and `metadata` and, if the user asked for a GitOps fix on a manifest-fixable cause, submit it via `submit-suggestion`.
- **Multi-cluster fleet sweep, or a cluster with no Cluster Agent (`exists: false`):** Run `leeway report` directly from this profile using the steps below (for a single cluster with `exists: false`, note in `result` that the cluster had no Cluster Agent).

## Step 1: Run `leeway report`

For a multi-cluster sweep across `<project>/<location>/<cluster>` triples, run `leeway report --topology-tier-c-signals --clusters`:

```bash
leeway report --topology-tier-c-signals --clusters "<project_a>/<location_a>/<cluster_a>,<project_b>/<location_b>/<cluster_b>"
```

When scanning a single cluster that has no Cluster Agent yet (`exists: false`), pin a per-target `KUBECONFIG` under `$HERMES_HOME/.kubeconfigs/` per `AGENTS.md` (exactly as in `gke-basics`), then run `leeway report --topology-tier-c-signals` (`leeway report` always outputs JSON (`leeway.report/v1alpha1`), and `--topology-tier-c-signals` includes unconstrained Tier C workloads alongside Tier A/B findings):

```bash
PROJECT="$GKE_PROJECT_ID"   # CLUSTER and LOCATION come from the request
export KUBECONFIG="${HERMES_HOME:-/opt/data}/.kubeconfigs/kubeconfig_${PROJECT}_${CLUSTER}_${LOCATION}.yaml"
gcloud container clusters get-credentials "$CLUSTER" --location="$LOCATION" --project="$PROJECT" --quiet
```

```bash
# Scan a specific namespace on the target cluster:
leeway report --topology-tier-c-signals --namespace <namespace>

# Scan all namespaces on the target cluster:
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

| Cause Category                                                                                                          | Signature in `leeway report` & Kubernetes                                                                                                                                                                                                                                                         | Confirming Read-Only Checks                                                                                                                                                                                                                                                             | Correct Remediation                                                                                                                                                                                                                                                                         |
| :---------------------------------------------------------------------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | :-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **1. Scheduling Misconfiguration** (e.g., `Deployment/checkout-api` with `ScheduleAnyway` + single-zone `nodeAffinity`) | All replicas are `Running` in one zone (`score.maxDomainShare == 1.0`, zero `Pending` pods). `suspectedCause` is `schedule_anyway_soft` or `affinity_conflict` (`whenUnsatisfiable: ScheduleAnyway` and/or single-zone `nodeAffinity`), while ready nodes matching the pool exist in other zones. | Inspect `spec.template.spec.topologySpreadConstraints` and `spec.template.spec.affinity.nodeAffinity` / `nodeSelector`. Verify that other zones have ready nodes in the target pool and that no PVCs or `Pending` pods are involved.                                                    | **Manifest edit:** remove or widen the single-zone `nodeAffinity` / `nodeSelector` to cover all cluster zones, and set `whenUnsatisfiable: DoNotSchedule` if strict zonal spread is required.                                                                                               |
| **2. Missing `topologySpreadConstraints`** (e.g., unconstrained `Deployment/frontend-web`)                              | All or most replicas are `Running` in one zone with zero `Pending` pods, `intent.source` is `"cluster_default"` or `"none"`, and the pod template declares no `topologySpreadConstraints` across `topology.kubernetes.io/zone`.                                                                   | Inspect `spec.template.spec.topologySpreadConstraints`. Confirm multiple zones have ready nodes with headroom and the workload mounts no zonal RWO PVCs.                                                                                                                                | **Manifest edit:** add a `topologySpreadConstraints` entry for `topology.kubernetes.io/zone` (`maxSkew: 1`) matching the workload's pod selector labels.                                                                                                                                    |
| **3. Zone-Pinned Storage (Volume)** (e.g., `StatefulSet/orders-db` on a zonal `StorageClass`)                           | Workload is a `StatefulSet` (or mounts RWO PVCs) with `suspectedCause: "volume_topology_locked"` (or `volumeClaimTemplates` referencing a zonal `StorageClass`), zero `Pending` pods, and all pods in one zone.                                                                                   | Check `PersistentVolumeClaim` and `StorageClass` (`kubectl get pvc -n <namespace>` and `kubectl get storageclass <storage_class_name> -o yaml`). Look for `allowedTopologies` pinning `topology.gke.io/zone` to one zone or zonal `pd.csi.storage.gke.io` disks bound in a single zone. | **Storage migration:** provision a regional `StorageClass` or migrate volume state across zones. Warn that editing `topologySpreadConstraints` to `DoNotSchedule` without migrating the zonal PVCs will break scheduling (`VolumeNodeAffinityConflict`).                                    |
| **4. Node Capacity Starvation** (e.g., `Deployment/batch-worker` with `Pending` pods on `Insufficient cpu`)             | `suspectedCause: "resource_starvation"` (or `Pending` pods coexist with `Running` pods concentrated in one zone with `score.maxDomainShare == 1.0`).                                                                                                                                              | Inspect the `Pending` pods and node allocatable capacity (`kubectl get pods -n <namespace> -o wide` and `kubectl describe pod <pending_pod_name> -n <namespace>`). Confirm `FailedScheduling` events citing `Insufficient cpu` or `Insufficient memory` in the starved zone.            | **Capacity or sizing change:** add node capacity in the starved zone, enable autoscaling, rebalance competing workloads in the full zone, or right-size container CPU/memory requests. Do **not** blame or edit the workload's `topologySpreadConstraints`.                                 |
| **5. Single-Zone Node Pool / Node-Count Asymmetry** (e.g., workload on a pool provisioned in one zone only)             | `suspectedCause: "node_count_asymmetry"`, `len(domains["topology.kubernetes.io/zone"]) == 1`, or the node pool targeted by the workload's `nodeSelector` / `nodeAffinity` has nodes in only one zone.                                                                                             | Check node zones per pool (`kubectl get nodes -L topology.kubernetes.io/zone,cloud.google.com/gke-nodepool`). Confirm no other zone has nodes in that pool.                                                                                                                             | **Capacity / node-pool change:** expand the node pool's `nodeLocations` across multiple zones (or provision a multi-zone node pool) via IaC/Config Connector. Never widen `nodeAffinity` or set `DoNotSchedule` onto zones that have no nodes in the pool, or moved pods will go `Pending`. |
| **6. Other / Unknown Cause** (`taint_blocked`, `daemonset_bound`, `unknown`)                                            | `suspectedCause` is `taint_blocked`, `daemonset_bound`, or `unknown`, or none of rows 1–5 matches the live evidence.                                                                                                                                                                              | Inspect node taints (`kubectl get nodes -o custom-columns=NAME:.metadata.name,TAINTS:.spec.taints`), workload tolerations, and controller type.                                                                                                                                         | **Withhold manifest spread edits:** report the blocking taint, controller constraint, or unverified cause and do not propose a `topologySpreadConstraints` patch until the underlying constraint is resolved.                                                                               |

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
3. **Attributed root cause:** explicitly classify the workload against the 6-row matrix above (`scheduling` / missing `topologySpreadConstraints`, `volume`, `capacity` starvation or single-zone node pool, or `other/unknown`).
4. **Tailored next step:** recommend a manifest change only for row 1 (scheduling misconfiguration) or row 2 (missing `topologySpreadConstraints`), a storage migration for row 3 (volume-pinned workloads), a capacity/node-pool or resource-sizing action for rows 4–5, and withhold manifest spread edits for row 6. When asked to propose a GitOps fix for a manifest-fixable scheduling cause, use the `submit-suggestion` skill. Never mutate live cluster resources directly.
