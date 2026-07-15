---
name: gke-obtainability-day1
description: Workflows for verifying compute capacity obtainability during initial workload rollout and cluster deployment (Day 1). Use when validating that GKE node pools, ComputeClass auto-provisioning, and autoscaling policies successfully acquire physical hardware capacity during rollout.
---

# GKE Obtainability: Day 1 (Deployment & Provisioning Verification)

This skill guides you through verifying capacity obtainability during initial workload deployments, cluster spin-ups, and node pool rollouts (`Day 1`). It provides structured diagnostic and validation steps to confirm that GKE node pools, `ComputeClass` auto-provisioning, and autoscalers successfully obtain physical Google Cloud hardware capacity without hitting quota ceilings or early scheduling bottlenecks.

## Verification Workflow

### Step 1: Verify Workload Rollout & Pod Scheduling States

Immediately after applying workload manifests or `ComputeClass` definitions, inspect the rollout progression and active pod phases across the cluster.

**Commands:**

```bash
# 1. Check deployment rollout progression
kubectl rollout status deployment/<workload_name> -n <namespace> --timeout=60s

# 2. Query any pods stuck in Pending phase
kubectl get pods -n <namespace> --field-selector=status.phase=Pending -o wide

# 3. Inspect recent scheduling events in the namespace
kubectl get events -n <namespace> --field-selector reason=FailedScheduling --sort-by='.metadata.creationTimestamp'
```

#### Diagnostic Decision Tree:

- **All Pods Running (`status.phase=Running`):** Capacity has been successfully obtained. Proceed to **Step 2**.
- **Pods Stuck in `Pending` (`FailedScheduling`):**
  - Look for message patterns: `0/X nodes are available: X Insufficient cpu / memory / nvidia.com/gpu`.
  - Check if Cluster Autoscaler or Node Auto-Provisioning (NAP) has triggered:
    ```bash
    kubectl get events -n kube-system --field-selector reason=TriggeredScaleUp --sort-by='.metadata.creationTimestamp'
    ```
  - If node provisioning is rejected right at initial deployment, transition immediately to the **Day 2 Stockout Investigation & Very Specific RCA Skill (`gke-obtainability-day2`)**.

---

### Step 2: Validate ComputeClass Auto-Provisioning & Node Creation

Confirm that `ComputeClass` specifications or auto-created node pools have initialized healthy underlying Google Compute Engine (GCE) instances.

**Commands:**

```bash
# 1. List active nodes allocated to the target ComputeClass
kubectl get nodes -l cloud.google.com/compute-class=<class_name> -o wide

# 2. Check node readiness and capacity allocations
kubectl describe nodes -l cloud.google.com/compute-class=<class_name> | grep -E "Name:|Allocated resources:|cpu |memory |nvidia.com/gpu"
```

#### Validation Checks:

- Confirm node zone distribution matches the Day 0 specification across multiple availability zones.
- Verify whether the provisioned instances are running on Spot capacity (`cloud.google.com/gke-spot=true`) or On-Demand fallback capacity (`cloud.google.com/gke-spot=false`).

---

### Step 3: Verify Quota & Capacity Reservation Consumption

Verify that newly provisioned nodes or workloads are actively drawing from designated capacity reservations and remain safely within Google Cloud project quotas.

**Commands:**

```bash
# 1. Inspect capacity reservation utilization across project regions
gcloud compute reservations list --project=<project_id> --format="table(name,zone,specificReservation.instanceProperties.machineType,specificReservation.count,specificReservation.inUseCount)"

# 2. Check active GCE regional quota headroom (e.g., CPUS, GPUS, IN_USE_ADDRESSES)
gcloud compute project-info describe --project=<project_id> --format="json(quotas)"
```

#### Validation Checks:

- **Reservation Affinity Match:** If the workload targets a capacity reservation (`SPECIFIC_RESERVATION`), verify that `specificReservation.inUseCount` has incremented by the expected node count. If `inUseCount` remains `0` while pods are pending, verify node pool `reservationAffinity` syntax.
- **Quota Headroom:** Ensure that total usage across `CPUS_ALL_REGIONS`, `NVIDIA_L4_GPUS`, or `PREEMPTIBLE_CPUS` remains below 85% of the total limit to allow safe autoscaling headroom.

---

## Day 1 Remediation Quick-Check

If verification fails during rollout:

1. **Missing Tolerations/Selectors:** Ensure the Pod manifest specifies `nodeSelector: cloud.google.com/compute-class: "<class-name>"` or valid tolerations for Spot/GPU taints.
2. **Invalid Priority Syntax:** Validate that the `ComputeClass` `machineFamily` and `gpu.type` combinations are supported in the selected target region (`gcloud compute accelerator-types list --filter="zone:<zone>"`).
3. **Escalate to Day 2:** If syntax and selectors are valid but instances fail to provision, execute the **Day 2 Stockout RCA (`gke-obtainability-day2`)** to classify zonal hardware exhaustion vs. quota ceilings.
