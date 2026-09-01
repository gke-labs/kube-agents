---
name: gke-obtainability
description: >-
  Check GKE quota and live hardware obtainability before recommending
  capacity. Use when a cluster design, capacity plan, or scale-up decision
  needs live evidence: regional quota verification (gcloud compute regions
  describe), reservations, and capacity obtainability advice (gcloud beta
  compute advice capacity / capacity-history) across zones and provisioning
  models. For handling an inbound cluster-autoscaler stockout alert end to
  end — triage, GitOps remediation, Pull Request — use the
  gke-stockout-investigator plugin skill instead.
metadata:
  category: Containers
---

# GKE Capacity Obtainability

Quota and capacity are different questions. Quota is a project limit you can
raise by asking; obtainability is whether the hardware is actually free in a
zone right now, and no amount of quota makes a stocked-out shape appear. This
skill gathers live evidence for both, records it in a form a reviewer can
verify, and states the distinction in the answer.

It is diagnosis and design only: nothing here creates a cluster, applies a
manifest, opens a Pull Request, or mutates cloud or Kubernetes state.

## How to use it

Loaded from a design, planning, or capacity-check request — for example the
[gke-cluster-creation](../gke-cluster-creation/SKILL.md) preflight:

- Run every check under **Diagnostics** below: quota, reservations, usage,
  and capacity advice.
- Run the capacity advice for **Spot and Flex-Start** — the two provisioning
  models `gcloud beta compute advice capacity` accepts
  (`--provisioning-model=SPOT` and `FLEX_START`) — and read the per-zone
  signals from each response. **On-Demand has no advance obtainability
  signal**: assess it through quota headroom and reservations, and say so.
  Your final answer must weigh all three paths — On-Demand, Spot, and
  Flex-Start — with their trade-offs.
- Report quota and live obtainability **separately**, and include this
  sentence verbatim in your final report: "Quota is separate from live
  capacity." Quota is a project limit; it does not prove hardware is
  obtainable, and obtainability evidence does not raise quota. **Never use
  the word "guarantee" about capacity, allocations, or scheduling anywhere
  in the report** — not even for reservations or ProvisioningRequests;
  write "reserves", "holds", or "provides once scheduled" instead.
- **Record what you executed as typed evidence** with the `record_evidence`
  tool — one record per check, built from the real command output, never
  from memory:
  - after the quota check: `type: quota_check` with the metric, limit,
    usage, and whether the request fits in `analysis`;
  - after the capacity advice calls: `type: advice_service_capacity` with
    `api_method: compute.beta.AdviceService.Capacity`. Use **exactly** these
    key names and shapes for `request` and `analysis` — do not rename keys,
    do not replace object entries with bare strings, fill the values from
    the real responses (probe at least two zones, with per-zone queries if
    one call returns fewer):

    ```json
    {
      "request": {
        "region": "us-central1",
        "acceleratorType": "nvidia-a100",
        "acceleratorCount": 32
      },
      "analysis": {
        "availableQuantity": 32,
        "zones": [
          {"zone": "us-central1-f", "obtainability": 0.9},
          {"zone": "us-central1-a", "obtainability": 0.5}
        ],
        "provisioningModels": {
          "SPOT": {"obtainability": 0.9, "zone": "us-central1-f"},
          "FLEX_START": {"status": "probed", "notes": "..."},
          "ON_DEMAND": {"source": "quota+reservations", "notes": "..."}
        }
      }
    }
    ```

  - after a server-side dry run of a generated ComputeClass
    (`kubectl apply --dry-run=server`): `type: computeclass_server_dry_run`.
- **Attach generated manifests as structured artifacts** with the
  `attach_artifact` tool — the parsed object, not YAML text:
  `type: computeclass` for a ComputeClass, `type: node_auto_provisioning`
  for a NAP specification; use one shared `pair_id` for a design's set.
- **Your final report must name every provisioning path by name** — the
  words On-Demand, Spot, and Flex-Start must each appear with their
  trade-off, even when a path's probe failed (then state the failure and
  what you used instead). A path you analyzed but never mentioned does not
  exist for the reader.
- **Generated manifests must use the real schemas.** Do not invent API
  versions or fields; start from these shapes and adjust values only:

  A GKE ComputeClass is `cloud.google.com/v1` (never `autopilot.gke.io/*`),
  `machineFamily` takes a family (`a2`), not a machine type, and GPU
  fallback tiers select accelerators via `gpu.type`:

  ```yaml
  apiVersion: cloud.google.com/v1
  kind: ComputeClass
  metadata:
    name: <design>-cc
  spec:
    priorities:
      - machineFamily: a2          # primary: the requested accelerator family
        spot: true
      - gpu:                       # fallback tier: smaller accelerator
          type: nvidia-l4
          count: 1
      - gpu:                       # last-resort tier
          type: nvidia-tesla-t4
          count: 1
    nodePoolAutoCreation:
      enabled: true
  ```

  Call out explicitly that the L4/T4 fallback tiers change the workload's
  GPU class and interconnect characteristics.

  A Node Auto-Provisioning alternative constrains machine families through
  node affinity, and its location policy lives under `location`:

  ```yaml
  kind: NodeAutoProvisioningSpec
  spec:
    nodeAffinity:
      requiredDuringSchedulingIgnoredDuringExecution:
        nodeSelectorTerms:
          - matchExpressions:
              - key: cloud.google.com/machine-family
                operator: In
                values: [n2, n2d, c2d]
    location:
      locationPolicy: ANY
  ```

- **Validate before attaching**: run `kubectl apply --dry-run=server -f` on
  the generated ComputeClass and record the outcome with
  `record_evidence(type: computeclass_server_dry_run)`; a manifest the API
  server rejects is a finding, not a deliverable.

## Diagnostics

#### A. Quota Verification

Verify that the proposed machine families, CPU, or GPU metric counts are within the region's quota limits:

```bash
gcloud compute regions describe us-central1 --format="json(quotas.filter(metric=CPUS))"
gcloud compute regions describe us-central1 --format="json(quotas.filter(metric=NVIDIA_L4_GPUS))"
```

_Note: Filter by other metric names (e.g., `N4_CPUS`, `C4_CPUS`, `NVIDIA_T4_GPUS`, `NVIDIA_A100_GPUS`) to inspect specific hardware._

#### B. Reservations Check

Check if any zonal reservations are available for the target workload's machine type; a reservation holds capacity for it (the report must not describe this as a guarantee):

```bash
gcloud compute reservations list --format="json"
```

#### C. Actual Workload Resource Usage

Before proposing resource reservations or changing VM shapes, analyze actual usage and account for potential spikes. Use:

```bash
# Get node CPU/memory utilization summary
kubectl top node

# Fetch raw metrics from the metrics API server
kubectl get --raw "/apis/metrics.k8s.io/v1beta1/nodes"

# Get pod CPU/memory utilization summary
kubectl top pod -n <namespace>
```

#### D. Spot VM Availability and Pricing Advice

If configuring fallback Spot instances or diagnosing GPU stockouts, use the Spot advice APIs to check obtainability and preemption risk across target zones:

1. **VM & GPU Availability Advice**:
   ```bash
   gcloud beta compute advice capacity \
       --provisioning-model=SPOT \
       --instance-selection-machine-types="g2-standard-4,g2-standard-12,n1-standard-4" \
       --target-distribution-shape=ANY \
       --size=1 \
       --region=us-central1 \
       --format="json"
   ```
2. **Preemption Rate and Price History**:
   ```bash
   gcloud beta compute advice capacity-history \
       --provisioning-model=SPOT \
       --machine-type=g2-standard-4 \
       --types=PREEMPTION,PRICE \
       --region=us-central1 \
       --format="json"
   ```

_MANDATE_: You MUST actually execute the quota check (`gcloud compute regions
describe`), the capacity advice (`gcloud beta compute advice capacity`), and,
where preemption history matters, `gcloud beta compute advice capacity-history`
— then record each as typed evidence and list the exact commands you ran in
your report. An analysis you did not execute is not evidence.

## ComputeClass resilience rules

These are the failure modes a fallback design has to survive. Check a design
you are proposing — or an existing ComputeClass you are reviewing — against
each of them.

#### Rule A: Lack of Zone/Family Fallbacks

- **Problem**: The ComputeClass `priorities[]` is pinned to a single machine family or a single zone, leaving no alternative when GCE encounters a stockout.
- **Fix**: Propose adding fallback priorities (additional machine families like `n4`, `c4`, `n2` or other zones within the region).

#### Rule B: Large VM Shape Scarcity (>32 vCPUs)

- **Problem**: The workload requests very large VMs (>32 vCPU) which draw from thinner capacity pools and are highly prone to stockouts.
- **Fix**:
  - If the workload is horizontally-scalable (e.g., stateless app with multiple replicas, batch job), propose updating the workload manifest to use smaller replicas (e.g., ≤32 vCPUs) and adding smaller-core fallback priorities to the ComputeClass.
  - If the workload is NOT horizontally-scalable (e.g., a single large monolithic database or inference server), do NOT shrink the shape. Instead, vary the machine family (e.g., fallback from C3 to N2/N4) and zones.

#### Rule C: Stateful Disk Generation Mix

- **Problem**: For stateful workloads using Persistent Volumes (PVs), Gen 2 VMs (e.g., `n2`, `n2d`) and Gen 4 VMs (e.g., `c4`, `n4` with Hyperdisk) are mixed in the same `priorities[]` array, causing PV attachment deadlocks.
- **Fix**: Remove the mixed generations. The priority list for a PV-attached workload must stick to all Gen 2 or all Gen 4 machine families.

#### Rule D: Missing On-Demand Floor

- **Problem**: The priority list contains only Spot instances without an On-Demand floor. If Spot is exhausted, the workload stays `Pending`.
- **Fix**: Add a lower-priority On-Demand priority rule at the end of the `priorities[]` array to act as a safety floor.

#### Rule E: Regional Scarcity (Specialized Hardware, e.g., GPUs/TPUs)

- **Problem**: The requested specialized hardware (e.g., Nvidia H100, L4, or TPU v5e) is completely stocked out across all zones in the target region.
- **Fix**: Recommend migrating the workload and its infrastructure to another GCP region where capacity is available, or changing the application architecture to use a more available hardware class.

#### Rule F: Regional Quota Exceeded Violation (`quota exceeded` / GPU Limit Cap)

- **Problem**: A workload requests more total resources (CPUs or GPUs) than the regional quota limit configured for the project in that region (e.g., requesting 32 L4 GPUs when `gcloud compute regions describe us-central1` shows the `NVIDIA_L4_GPUS` quota limit is 24).
- **Fix**: Identify this explicitly as a **Regional Quota Exceeded Violation** in the diagnosis. Propose adjusting the workload deployment manifest to cap total requested GPUs/CPUs to fit strictly within the regional quota limit (e.g. reducing replicas from 4 to 3 so total GPUs = 24), and create a `ComputeClass` providing multi-zone fallback capabilities.

#### Rule G: CCC Priority Starvation & Reset Loop (Excessive Granular Machine Types)

> [!IMPORTANT]
> **MANDATORY PRIORITY CHECK**: If a ComputeClass `priorities[]` list contains more than 10 granular `machineType` rules (e.g., 25 priority rules for specific machine shapes like `n2-standard-4`, `n2-standard-8`, etc.), this is a Rule G violation. You MUST NOT add more `machineType` rules. Instead, you MUST auto-compress the configuration by replacing ALL 25 granular `machineType` rules with 4 family-level (`machineFamily`) rules (e.g., `n4`, `c3`, `n2`, `e2`).

- **Problem**: A Custom Compute Class (CCC) contains excessive granular `machineType` rules (e.g., 25 priority rules for specific machine shapes), exceeding Flex Advisor's cache limit (generating >200 combinations) and triggering a Cluster Autoscaler backoff reset loop. Lower-priority fallbacks (`n2`, `e2`) are starved and pods remain stuck in `Pending`.
- **Fix**: Auto-compress the CCC configuration: Completely REPLACE the entire list of specific granular machine sizes (`machineType`) with 4 family-level definitions (`machineFamily`: `n4`, `c3`, `n2`, `e2`), reducing priority rules from 25 to 4 family-level priorities and avoiding the starvation loop.

#### Rule H: Hyperdisk Incompatibility with Older Generation Machines

- **Problem**: A workload using Hyperdisk (e.g. `hyperdisk-balanced`, `hyperdisk-throughput`, `hyperdisk-extreme`, or StorageClass with hyperdisk CSI provisioner) uses a CCC definition whose 1st choice is a 3rd/4th generation machine type (e.g. `c3-standard-4`, `c4-standard-4`), but has fallbacks to older generation machine types (e.g. `c2`, `n2`, `e2`). Once there is a stockout on the 1st choice, Cluster Autoscaler falls back to an incompatible machine type (`c2`, `n2`, `e2`) that does not support Hyperdisk, causing scale-up to fail.
- **Fix**: Increase CCC fallback options to other machine families compatible with Hyperdisk (e.g. `c3`, `c4`, `n4`, `c3d`), and remove fallbacks which do not work with Hyperdisk (`c2`, `n2`, `e2`).
