---
name: gke-obtainability-day2
description: Systematic Standard Operating Procedure (SOP) for Day 2 operations: investigating GKE compute stockouts (FailedScheduling, ZONE_RESOURCE_POOL_EXHAUSTED) and conducting granular Root Cause Analysis (RCA) and GitOps remediation.
---

# GKE Obtainability: Day 2 (Stockout Investigation & Very Specific RCA)

Use this skill during active Day 2 production operations when Google Kubernetes Engine (GKE) workloads fail to scale or schedule due to capacity constraints (`FailedScheduling`, `ZONE_RESOURCE_POOL_EXHAUSTED`, `Scale-up failed`). This skill enforces a deep-dive cloud infrastructure investigation and delivers a **Very Specific Root Cause Analysis (RCA)** before generating precise GitOps YAML remediation patches.

---

## 🔍 Diagnostic Investigation Workflow

### Step 1: Inspect Pod Scheduling Rejection Events

When pods remain stuck in `Pending` state, capture the exact scheduling error and node availability breakdown.

**Diagnostic Commands:**

```bash
# 1. Query pods stuck in Pending state
kubectl get pods -n <namespace> --field-selector=status.phase=Pending -o wide

# 2. Extract precise FailedScheduling event strings
kubectl get events -n <namespace> --field-selector reason=FailedScheduling --sort-by='.metadata.creationTimestamp'
```

- If events report `0/X nodes are available: X Insufficient cpu / memory / nvidia.com/gpu`, the cluster lacks active node headroom and is attempting to provision new instances via Cluster Autoscaler or `ComputeClass` Node Auto-Provisioning. Proceed immediately to **Step 2**.

---

### Step 2: Deep-Dive Cloud Logging for GCE API Errors (`ZONE_RESOURCE_POOL_EXHAUSTED`)

Query Google Cloud Logging directly to inspect underlying Google Compute Engine (GCE) API rejections and Cluster Autoscaler scale-up failures within a 1-hour window around the failure timestamp.

**Diagnostic Command:**

```bash
gcloud logging read 'resource.type="k8s_cluster" AND (jsonPayload.reason="ZONE_RESOURCE_POOL_EXHAUSTED" OR jsonPayload.reason="ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS" OR jsonPayload.reason="QUOTA_EXCEEDED" OR textPayload=~"Scale-up failed" OR textPayload=~"NotTriggeredScaleUp")' --limit=25 --format="json" --project=<project_id>
```

#### Key Payload Fields to Extract:

- `jsonPayload.reason`: Exact rejection code (e.g., `ZONE_RESOURCE_POOL_EXHAUSTED`).
- `jsonPayload.resourceDescription.machineType` or `machineFamily`: Exact VM family rejected (`g2-standard-8`, `nvidia-l4`).
- `jsonPayload.resourceDescription.zone`: Exact data center zone where the stockout occurred (`us-central1-a`).

---

### Step 3: Audit GCP Project Quotas vs. Active Usage

Verify whether the scale-up rejection is caused by a project API quota ceiling rather than a zonal hardware shortage.

**Diagnostic Command:**

```bash
gcloud compute project-info describe --project=<project_id> --format="json(quotas)"
```

- Filter and inspect relevant limits (`CPUS_ALL_REGIONS`, `NVIDIA_L4_GPUS`, `PREEMPTIBLE_CPUS`, `IN_USE_ADDRESSES`). Compare `usage` against `limit`.

---

### Step 4: Audit Capacity Reservation Status & Affinity

Check if the project has guaranteed, pre-paid idle capacity reserved (`gcloud compute reservations`) that the GKE workloads or node pools are failing to consume due to incorrect affinity settings.

**Diagnostic Command:**

```bash
gcloud compute reservations list --project=<project_id> --format="table(name,zone,specificReservation.instanceProperties.machineType,specificReservation.count,specificReservation.inUseCount)"
```

---

## 🚨 Granular Root Cause Classification (Very Specific RCA Decision Tree)

Based on the evidence collected in Steps 1–4, classify the root cause into **one of the four exact buckets below**. Never provide generic or ambiguous advice. Always format your output with clear proof and exact actionable fixes.

### Bucket 1: True Zonal Hardware Stockout (`ZONE_RESOURCE_POOL_EXHAUSTED`)

- **Evidence:** Cloud Logging explicitly reports `ZONE_RESOURCE_POOL_EXHAUSTED` (or `_WITH_DETAILS`) for machine type `$MACHINE_TYPE` (`$RESOURCE`) in zone `$ZONE`. Project quota has available headroom.
- **Very Specific RCA Output:**
  > 🔴 **Hardware Stockout Confirmed:** Google Cloud physical data center zone `$ZONE` currently has 0 available instances of `$MACHINE_TYPE` (`$RESOURCE`).
- **Actionable Remediation:**
  1. Generate a GitOps YAML patch for the target `ComputeClass` adding secondary fallback zones (`$REGION-b`, `$REGION-c`) or alternative machine families (`c3`, `a2`) to `spec.priorities`.
  2. If running on standard node pools, recommend deploying a secondary regional or multi-zonal node pool.

### Bucket 2: Spot VM Preemption / Exhaustion Without Fallback

- **Evidence:** Workload or `ComputeClass` strictly demands Spot instances (`cloud.google.com/gke-spot: "true"` or `spot: true` only). Cloud Logging shows Spot scale-up rejection (`ZONE_RESOURCE_POOL_EXHAUSTED` on preemptible pool), and no On-Demand fallback priority exists.
- **Very Specific RCA Output:**
  > 🟡 **Spot Pool Exhaustion:** Spot VM capacity for `$MACHINE_TYPE` in `$ZONE` is exhausted, and the workload/ComputeClass lacks an On-Demand (`spot: false`) fallback rule.
- **Actionable Remediation:**
  1. Generate a GitOps YAML patch adding a secondary priority level targeting `spot: false` within the `ComputeClass`.
  2. Ensure `spec.activeMigration.optimizeRulePriority: true` is enabled so the workload automatically returns to Spot when capacity recovers.

### Bucket 3: GCP Project Quota Ceiling Hit (`QUOTA_EXCEEDED`)

- **Evidence:** Cloud Logging reports `QUOTA_EXCEEDED` or `Limit '$QUOTA_NAME' exceeded`. `gcloud compute project-info describe` confirms `usage == limit` for that resource (`$QUOTA_NAME`).
- **Very Specific RCA Output:**
  > 🟠 **Project Quota Ceiling Exceeded:** This is NOT a hardware stockout. Your GCP project `$PROJECT_ID` has reached its API quota limit of `$LIMIT` for `$QUOTA_NAME`.
- **Actionable Remediation:**
  1. Provide the exact instructions to request a quota increase via Google Cloud Console (`IAM & Admin > Quotas`).
  2. If quota is available in an adjacent region, generate a recommendation to shift non-critical workloads or multi-region deployments to that region.

### Bucket 4: Capacity Reservation Mismatch (`reservationAffinity`)

- **Evidence:** `gcloud compute reservations list` reveals idle capacity (`specificReservation.inUseCount < specificReservation.count`) for the required machine type/GPU, but `kubectl describe node/pod` reveals `reservationAffinity: NO_RESERVATION` or a mismatched reservation name.
- **Very Specific RCA Output:**
  > 🟣 **Reservation Mismatch:** `$IDLE_COUNT` idle `$MACHINE_TYPE` instances exist in capacity reservation `$RESERVATION_NAME`, but the workload/ComputeClass is configured with `reservationAffinity: NO_RESERVATION` and cannot consume them.
- **Actionable Remediation:**
  1. Generate a GitOps YAML patch updating the `ComputeClass` or node pool configuration to set `consumeReservationType: SPECIFIC_RESERVATION` pointing directly to `$RESERVATION_NAME`.

---

## Step 5: GitOps Remediation Boundary

Following the inviolable GitOps boundary of `kube-agents`:

1. **Never apply patches directly to live production clusters.**
2. Synthesize your **Very Specific RCA** clearly in your report.
3. Generate the corrected YAML manifest patch corresponding to the identified bucket.
4. Open or update a Git branch and Pull Request (PR) on GitHub containing the remediation patch for human SRE review and merge.
