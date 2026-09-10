---
name: openshift-gcp-obtainability
description: >-
  Orchestrates resource obtainability, zonal stockout recovery, and machine type fallbacks for OpenShift clusters running on Google Compute Engine (GCE). Trigger when OpenShift MachineSets fail to provision nodes due to ZONE_RESOURCE_POOL_EXHAUSTED or QUOTA_EXCEEDED, when pods remain Pending with FailedScheduling, or when designing stockout-resilient machine topologies.
metadata:
  category: Containers
---

# OpenShift GCP Node Obtainability & Stockout Recovery

Guidance on detecting GCE stockouts, querying Google Cloud Obtainability APIs, and automating OpenShift `MachineSet` failovers across zones and machine families.

## Critical Rules

- **STOCKOUT COOLDOWN IN OPENSHIFT:** OpenShift's ClusterAutoscaler backs off exponentially when a MachineSet fails to provision. Never let a stocked-out MachineSet remain the sole autoscaling target. Immediately deploy or enable an alternative zonal or multi-family MachineSet.
- **READ-ONLY DIAGNOSTICS & GITOPS MUTATIONS:** The Platform Agent operates with read-only cluster visibility. Never execute direct `oc delete` or `apply` commands against cluster infrastructure. Propose all MachineSet changes as GitOps pull requests or via `submit-suggestion` for operator review.
- **CLI COMPATIBILITY:** Machine API resources are standard Custom Resources. Commands run identically via `oc` or `kubectl` with the full group (e.g. `kubectl get machines.machine.openshift.io -n openshift-machine-api`).
- **NO DESTRUCTIVE MUTATIONS WITHOUT REVIEW:** When proposing a MachineSet failover, present the new MachineSet YAML as a GitOps pull request or interactive recommendation. Do not delete active nodes running workloads.
- **SPOT/PREEMPTIBLE FALLBACK:** If a Spot MachineSet (`preemptible: true`) experiences stockouts, do not simply retry. Propose scaling an on-demand MachineSet with matching labels and tolerations.
- **QUOTA PRE-FLIGHT:** Before synthesizing fallback MachineSets, verify that the project's regional Compute Engine quotas (e.g. `CPUS`, `N2_CPUS`, `NVIDIA_A100_GPUS`) can accommodate the new capacity.

---

## Detection & Diagnosis Workflow

### Step 1: Detect Machine Provisioning Stockouts

Scan OpenShift `Machine` objects in namespace `openshift-machine-api` for provisioning failures:

```bash
kubectl get machines.machine.openshift.io -n openshift-machine-api -o json | jq -r '
  .items[] | select(.status.phase == "Failed" or .status.errorMessage != null) |
  "Machine: \(.metadata.name) | Zone: \(.spec.providerSpec.value.zone) | Error: \(.status.errorMessage)"
'
```

Common error signatures:

- `ZONE_RESOURCE_POOL_EXHAUSTED`: The requested GCE machine type or accelerator is out of stock in that specific zone.
- `QUOTA_EXCEEDED`: The GCP project has reached its regional vCPU or GPU quota.
- `RESOURCE_NOT_FOUND`: The requested machine family is not available in that zone.

### Step 2: Query GCE Capacity Advice

When a stockout is detected, query the GCE Advice API (`compute.alpha.AdviceService.Capacity` or `GeneralCapacityRecommendation`) for available alternative zones and machine families:

- Primary shape constrained: e.g. `n4-standard-8` in `us-central1-a`.
- Recommendation: Switch to `n2-standard-8` in `us-central1-a`, or shift `n4-standard-8` to `us-central1-b` or `us-central1-f`.

---

## Remediation Patterns on OpenShift

### Pattern 1: Multi-Zone MachineSet Failover

OpenShift `MachineSet`s are zonal. When `us-central1-a` is exhausted:

1. Identify the stocked-out `MachineSet` directly from the failed `Machine` (using its `cluster-api-machineset` label or matching the exhausted zone):
   ```bash
   # Direct derivation from the failing Machine identified in the Detection step:
   FAILED_MS=$(kubectl get machine.machine.openshift.io <FAILED_MACHINE_NAME> -n openshift-machine-api -o jsonpath='{.metadata.labels.machine\.openshift\.io/cluster-api-machineset}')

   # Or query by matching the exhausted zone:
   FAILED_MS=$(kubectl get machinesets.machine.openshift.io -n openshift-machine-api -o json | jq -r --arg zone "$EXHAUSTED_ZONE" '.items[] | select(.spec.template.spec.providerSpec.value.zone == $zone) | .metadata.name')
   ```
2. Clone the `MachineSet` manifest, altering:
   - `metadata.name`: Replace zone suffix with the healthy target zone (e.g. `us-central1-b`).
   - `spec.template.spec.providerSpec.value.zone`: Set to the healthy zone.
3. Propose the fallback `MachineSet` and `MachineAutoscaler` manifests via a GitOps pull request or `submit-suggestion`.
4. Scale down or pause the stocked-out `MachineSet` via GitOps to halt autoscaler backoff thrashing.

### Pattern 2: Machine Family Tiering (e.g. N4 -> N2 / C2D)

If an entire region is constrained for newer machine families (e.g. N4, C3, or A2 GPUs):

1. Create a secondary `MachineSet` utilizing established, widely available machine types (e.g. `n2-standard-8` or `c2d-standard-8`).
2. Ensure the worker node labels and taints match the primary pool so pending pods can schedule without requiring pod specification modifications.

### Pattern 3: Automated Machine Cleanup

Clean up stuck `Machine` objects that block cluster autoscaler progress only with operator confirmation or via GitOps:

```bash
# Operator confirmation required:
oc delete machine <STUCK_MACHINE_NAME> -n openshift-machine-api
```
Do not execute destructive machine deletion commands unattended.
