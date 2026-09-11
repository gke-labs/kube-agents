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
- **CLI & CREDENTIAL PROXY COMPATIBILITY:** In OpenShift-managed clusters, commands execute via `oc` or `kubectl` against the cluster API. On installations using the credential-proxy shim, OpenShift Custom Resources require cluster API routing. When reading resources directly, use standard Custom Resource paths (`kubectl get machines.machine.openshift.io -n openshift-machine-api` or `oc get machines -n openshift-machine-api`).
- **NO DESTRUCTIVE MUTATIONS WITHOUT REVIEW:** When proposing a MachineSet failover, present the new MachineSet YAML as a GitOps pull request or interactive recommendation. Do not delete active nodes running workloads.
- **SPOT/PREEMPTIBLE FALLBACK:** If a Spot MachineSet (`preemptible: true`) experiences stockouts, do not simply retry. Propose scaling an on-demand MachineSet with matching labels and tolerations.
- **QUOTA PRE-FLIGHT:** Before synthesizing fallback MachineSets, verify that the project's regional Compute Engine quotas (e.g. `CPUS`, `N2_CPUS`, `NVIDIA_A100_GPUS`) can accommodate the new capacity.

---

## Detection & Diagnosis Workflow

### Step 1: Detect Machine Provisioning Stockouts

Scan OpenShift `Machine` objects in namespace `openshift-machine-api` for provisioning failures:

```bash
{ oc get machines -n openshift-machine-api -o json 2>/dev/null || kubectl get machines.machine.openshift.io -n openshift-machine-api -o json; } | jq -r '
  .items[] | select(.status.phase == "Failed" or .status.errorMessage != null) |
  "Machine: \(.metadata.name) | Zone: \(.spec.providerSpec.value.zone) | Error: \(.status.errorMessage)"
'
```

Common error signatures:

- `ZONE_RESOURCE_POOL_EXHAUSTED`: The requested GCE machine type or accelerator is out of stock in that specific zone.
- `QUOTA_EXCEEDED`: The GCP project has reached its regional vCPU or GPU quota.
- `RESOURCE_NOT_FOUND`: The requested machine family is not available in that zone.

### Step 2: Query Google Cloud Spot & Flex Obtainability APIs

When detecting a stockout, scaling Spot pools, or qualifying MachineSet machine types, query the GCE Advice APIs via `gcloud` (available directly or via the credential proxy) or REST:

1. **Spot / Flex Capacity Advice:**
   Query obtainability score (0.0–1.0) and recommended zones across candidate machine shapes:

   ```bash
   gcloud beta compute advice capacity \
     --project="<PROJECT_ID>" \
     --region="<REGION>" \
     --provisioning-model=SPOT \
     --size=1 \
     --instance-selection-machine-types="n2-standard-4,n4-standard-4,c2d-standard-4" \
     --target-distribution-shape=any \
     --format=json
   ```

   Parse `.recommendations[].scores.obtainability` and `.recommendations[].shards[]` for optimal zone and machine family. For Flex-start workloads, specify `--provisioning-model=FLEX_START`.

2. **Spot Preemption & Price History:**
   Evaluate preemption volatility before provisioning Spot MachineSets:

   ```bash
   gcloud beta compute advice capacity-history \
     --project="<PROJECT_ID>" \
     --region="<REGION>" \
     --provisioning-model=SPOT \
     --machine-type="<MACHINE_TYPE>" \
     --types=PREEMPTION,PRICE \
     --format=json
   ```

   Flag recent `.preemptionHistory[].preemptionRate > 0.15` as elevated risk requiring multi-zone spread or on-demand tiering.

3. **REST API Equivalent (via Credential Proxy):**
   ```bash
   curl -s -H "Authorization: Bearer $(cat ${CREDENTIAL_PROXY_TOKEN_FILE:-/var/run/secrets/kubeagents/credential-proxy/token})" \
     -H "Content-Type: application/json" \
     "https://compute.googleapis.com/compute/beta/projects/<PROJECT_ID>/regions/<REGION>/advice/capacity" \
     -d '{"distributionPolicy":{"targetShape":"ANY"},"instanceFlexibilityPolicy":{"instanceSelections":{"selection-1":{"machineTypes":["n2-standard-4","n4-standard-4"]}}},"instanceProperties":{"scheduling":{"provisioningModel":"SPOT"}},"size":1}'
   ```

---

## Remediation Patterns on OpenShift

### Pattern 1: Multi-Zone MachineSet Failover

OpenShift `MachineSet`s are zonal. When `us-central1-a` is exhausted:

1. Identify the stocked-out `MachineSet` directly from the failed `Machine` (using its `cluster-api-machineset` label or matching the exhausted zone):
   ```bash
   # Direct derivation from the failing Machine identified in the Detection step:
   FAILED_MS=$(oc get machine <FAILED_MACHINE_NAME> -n openshift-machine-api -o jsonpath='{.metadata.labels.machine\.openshift\.io/cluster-api-machineset}' 2>/dev/null || kubectl get machine.machine.openshift.io <FAILED_MACHINE_NAME> -n openshift-machine-api -o jsonpath='{.metadata.labels.machine\.openshift\.io/cluster-api-machineset}')

   # Or query by matching the exhausted zone:
   FAILED_MS=$({ oc get machinesets -n openshift-machine-api -o json 2>/dev/null || kubectl get machinesets.machine.openshift.io -n openshift-machine-api -o json; } | jq -r --arg zone "$EXHAUSTED_ZONE" '.items[] | select(.spec.template.spec.providerSpec.value.zone == $zone) | .metadata.name')
   ```
2. Clone the `MachineSet` manifest, altering:
   - `metadata.name`: Replace zone suffix with the healthy target zone (e.g. `us-central1-b`).
   - `spec.template.spec.providerSpec.value.zone`: Set to the healthy zone.
3. Propose the fallback `MachineSet` and `MachineAutoscaler` manifests via a GitOps pull request or `submit-suggestion`.
4. Scale down or pause the stocked-out `MachineSet` via GitOps to halt autoscaler backoff thrashing.

### Pattern 2: Spot/Flex Qualification & Resilient MachineSet Sizing

Before provisioning OpenShift Spot MachineSets (`spec.template.spec.providerSpec.value.preemptible: true`):

1. Run Step 2's capacity advice query across candidate machine types and zones.
2. Select the zone and machine family with `.scores.obtainability >= 0.8` and `.preemptionRate <= 0.15`.
3. If obtainability in the primary zone is `< 0.5`, failover to the API-recommended secondary zone.
4. If Spot obtainability is constrained across all shapes, propose an on-demand fallback MachineSet with matching labels and taints.

### Pattern 3: Machine Family Tiering (e.g. N4 -> N2 / C2D)

If an entire region is constrained for newer machine families (e.g. N4, C3, or A2 GPUs):

1. Create a secondary `MachineSet` utilizing established, widely available machine types (e.g. `n2-standard-8` or `c2d-standard-8`).
2. Ensure the worker node labels and taints match the primary pool so pending pods can schedule without requiring pod specification modifications.

### Pattern 4: Automated Machine Cleanup

Clean up stuck `Machine` objects that block cluster autoscaler progress only with operator confirmation or via GitOps:

```bash
# Operator confirmation required:
oc delete machine <STUCK_MACHINE_NAME> -n openshift-machine-api
```

Do not execute destructive machine deletion commands unattended.
