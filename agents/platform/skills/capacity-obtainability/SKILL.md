---
name: capacity-obtainability
description: >-
  Check GKE quota and live hardware obtainability before recommending
  capacity, and plan future capacity windows for deadline-bound batch jobs.
  Use when a cluster design, capacity plan, scale-up decision, GPU/TPU batch
  schedule, or a free-standing availability question needs live evidence:
  regional quota verification (gcloud compute regions describe),
  reservations, capacity obtainability advice (gcloud beta compute advice
  capacity / capacity-history) across zones and provisioning models, and
  future window advice (gcloud beta compute advice calendar-mode) for "when
  will this shape be obtainable". For handling an inbound cluster-autoscaler
  stockout alert end to end — triage, GitOps remediation, Pull Request — use
  the gke-stockout-investigator plugin skill instead.
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
[gke-cluster-creation](../gke-cluster-creation/SKILL.md) preflight.

### Run the checks

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

### Record what you executed as typed evidence

Use the `record_evidence` tool — one record per check, built from the real
command output, never from memory. **"Design-only", "planning-only", and
"text output only" in a task never waive these records**: those
constraints forbid mutations — applying, submitting, creating — and the
typed records are not mutations, they are how the work is delivered. A
task that asks for the answer "as text" still gets its `record_evidence`
call per check and its `attach_artifact` call per manifest, alongside the
prose; skipping them delivers a claim, not evidence. If `record_evidence`
or `attach_artifact` is not among your tools, do not stop and do not skip
the check: put the same
JSON under an `## Evidence` heading in your report, and the manifests as
fenced YAML there, so the record still reaches the reader.

- after the quota check: `type: quota_check` with the metric, limit, usage,
  whether the request fits, and the reservations found, in `analysis` — this
  record is the On-Demand assessment;
- after the capacity advice calls: `type: advice_service_capacity` with
  `api_method: compute.beta.AdviceService.Capacity`, shaped exactly as below.

For `advice_service_capacity`, use **exactly** these key names and shapes for
`request` and `analysis` — do not rename keys, do not replace object entries
with bare strings, fill the values from the real responses. **Cover at least
two zones**: a region-level call with `ANY` reports its single best zone, so
when it returns fewer than two, run the per-zone follow-up in Diagnostics D
(`--zones=<zone>`) for the region's other zones and record each one — a low
score is still a signal and stays in `zones`; a zone the probe reports as
unsupported goes in `analysis.zoneStatuses` (`{"us-central1-b":
"NOT_SUPPORTED"}`). The recorder refuses a completed record covering fewer
than two. Only the two models the API returns belong in this record;
On-Demand is assessed from the quota and reservation checks and reported
under its own path below:

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
      { "zone": "us-central1-f", "obtainability": 0.9 },
      { "zone": "us-central1-a", "obtainability": 0.5 }
    ],
    "provisioningModels": {
      "SPOT": { "obtainability": 0.9, "zone": "us-central1-f" },
      "FLEX_START": { "status": "probed", "notes": "..." }
    }
  }
}
```

### Attach generated manifests as structured artifacts

Use the `attach_artifact` tool — the parsed object, not YAML text:
`type: computeclass` for a ComputeClass, `type: node_auto_provisioning` for a
NAP specification; use one shared `pair_id` for a design's set.

### Name all three provisioning paths in the report

Your final report must carry this section, with all three paths named — a
path you analyzed but never mentioned does not exist for the reader, and a
probe that failed is a finding, not a gap to leave silent:

```markdown
## Provisioning paths

- **On-Demand** — <quota headroom and reservations; no advance
  obtainability signal exists for this path>
- **Spot** — <obtainability score and zone, and the preemption trade-off>
- **Flex-Start** — <obtainability for the run duration, or, if the probe
  failed, what failed and what you relied on instead>
```

### Generated manifests must use the real schemas

Do not invent API versions or fields; start from these shapes and adjust
values only. There is no cluster to validate against on a Day-0 design, and
the agent's Kubernetes grant is read-only, so the shapes below are the check:
a manifest that departs from them is a finding, not a deliverable.

A GKE ComputeClass is `cloud.google.com/v1` (never `autopilot.gke.io/*`),
`machineFamily` takes a family (`a2`), not a machine type, and GPU fallback
tiers select accelerators via `gpu.type`. The order follows the
[gke-compute-classes](../gke-compute-classes/SKILL.md) AI/ML rule and Rule D
below: On-Demand (or a reservation) on the requested family first, Spot on
the same family second, never Spot as the primary tier:

```yaml
apiVersion: cloud.google.com/v1
kind: ComputeClass
metadata:
  name: <design>-cc
spec:
  priorities:
    - machineFamily: a2 # primary: the requested family, On-Demand
    - machineFamily: a2 # same family on Spot, behind the floor
      spot: true
    - gpu: # fallback tier: smaller accelerator
        type: nvidia-l4
        count: 1
    - gpu: # last-resort tier
        type: nvidia-tesla-t4
        count: 1
  nodePoolAutoCreation:
    enabled: true
```

Call out explicitly that the L4/T4 fallback tiers change the workload's GPU
class and interconnect characteristics.

A Node Auto-Provisioning alternative constrains machine families through node
affinity, and its location policy lives under `location`:

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

## Future windows (batch jobs with a deadline)

Run this section when the request is a job, not a cluster: a given shape and
count that must run for a given duration inside a horizon ("64 TPU v5e nodes,
12 hours, within 48 hours"). It answers _when_ and _where_ the shape is
predicted obtainable, with Diagnostics E below; the quota check (Diagnostics
A) still runs first, because a window you lack quota for is not a plan.

Facts the probe rests on, verified against the live API — do not guess past
them:

- **The API counts chips, not nodes.** A v5e host carries 4 chips, so 64
  nodes is `--chip-count=256`. State the conversion arithmetic in the
  report body itself — "64 nodes × 4 chips per host = 256 chips" — and
  record both numbers in the evidence, so a reader can check the
  arithmetic without opening the probe.
- **The minimum reservable window is one day.** A `--duration-range` under
  `min=1d` returns `CONDITIONS_NOT_MET` ("The time window is too short") in
  every zone. Reserve the day and run the job inside it: the job's own
  duration goes in the report and the ProvisioningRequest
  (`maxRunDurationSeconds`), never in the probe's duration range.
- **One call per candidate region, one recommended window per call.** A
  success carries `startTime`, `endTime`, and `location` for the best window
  in that region, plus a per-zone status map (`otherLocations`) for the
  rest: `NO_CAPACITY`, `NOT_SUPPORTED`, or `CONDITIONS_NOT_MET` with
  details. A zone that does not support the shape is a finding to report,
  not a retry.
- **The deadline bounds the start.** A job of duration D with horizon H
  cannot start later than H − D after now; pass exactly that as
  `--start-time-range` (compute the range immediately before the call, not
  at the start of the conversation). A 12-hour job with a 48-hour horizon
  cannot start later than hour 36.

**Ranking.** Collect every region's recommended window, then rank: earliest
start first; ties break toward the zone with the larger quota headroom from
Diagnostics A. Ranks are unique and consecutive from one. The recommendation
names the rank-one window's zone and exact UTC start time, and lists the
runner-up windows and every zone that returned no window, with its status.
If no allowed zone returns a window, say so and report each zone's status —
an honest "no window inside the horizon" is the answer, not a failure to
hide.

**Record the evidence.** One `type:
advice_service_workload_obtainability_planning` record per region call.
Use **exactly** this `request` shape — do not rename keys and do not
restate the gcloud flags as a flat dictionary; `futureResourcesSpecs` is
the API's body, reconstructed from the flags you passed (the shell gate
refuses `--log-http`, so capturing the wire body is not a path you have).
A v5e host carries 4 chips and the reservation floor is one day, so for 64
nodes the aggregate is 256 chips and the durations are `86400s`:

```json
{
  "region": "europe-west4",
  "nodeCount": 64,
  "chipsPerNode": 4,
  "futureResourcesSpecs": {
    "spec": {
      "deploymentType": "DENSE",
      "locationPolicy": {
        "locations": { "zones/europe-west4-b": { "preference": "ALLOW" } }
      },
      "targetResources": {
        "aggregateResources": {
          "acceleratorCount": 256,
          "vmFamily": "VM_FAMILY_CLOUD_TPU_LITE_POD_SLICE_CT5LP",
          "workloadType": "BATCH"
        }
      },
      "timeRangeSpec": {
        "minDuration": "86400s",
        "maxDuration": "86400s",
        "startTimeNotEarlierThan": "<now, RFC 3339 UTC>",
        "startTimeNotLaterThan": "<now + horizon - job duration>"
      }
    }
  }
}
```

Set `api_method: compute.beta.AdviceService.CalendarMode` and put the
structured findings from the real response in `analysis`. Then exactly one
`type: workload_obtainability_planning_analysis` record whose `analysis`
carries two keys: `windows`, every schedulable window **in rank order,
rank one first**, each with `region`, `zone`, `startTime`, `endTime`,
`durationHours` (the job's, not the reservation's), `capacitySignal` (the
zone's status or `RECOMMENDED`), and its `rank`; and `zoneStatuses`, one
entry per allowed zone that returned no window, mapping the zone to the
status the API gave (`NO_CAPACITY`, `NOT_SUPPORTED`, ...), so the analysis
covers every allowed zone whether or not it is obtainable.

**Attach the paired manifests.** A Dynamic Workload Scheduler request and
its Kueue queue, both under one `pair_id`, both carrying a `target` of the
rank-one window's `region`, `zone`, and `startTime`, and both in the same
namespace. The shapes are pinned; adjust values only:

```yaml
apiVersion: autoscaling.x-k8s.io/v1
kind: ProvisioningRequest
metadata:
  name: <job>-window
  namespace: <namespace>
  annotations:
    obtainability.kube-agents/zone: <rank-one zone>
    obtainability.kube-agents/start: "<rank-one startTime, UTC>"
spec:
  provisioningClassName: queued-provisioning.gke.io
  parameters:
    maxRunDurationSeconds: "43200" # the job's run, not the reservation day
  podSets:
    - count: 64 # nodes, not chips
      podTemplateRef:
        name: <job>-pod-template
```

```yaml
apiVersion: kueue.x-k8s.io/v1beta1
kind: LocalQueue
metadata:
  name: <job>-queue
  namespace: <namespace>
  annotations:
    obtainability.kube-agents/zone: <rank-one zone>
spec:
  clusterQueue: <cluster-queue>
```

Attach each with `attach_artifact` (`type: provisioning_request`,
`type: local_queue`) as parsed objects with `machineSpec` (for a TPU job,
`acceleratorType: tpu-v5e` and the chip arithmetic), `pair_id`, and
`target`. Planning only: hand both manifests to the user, apply nothing,
submit nothing.

**Anchor the probe at the clock, not at a buffer.** The range's `from` is
now — the moment you run the command — not now plus a safety margin; the
API's own window search supplies any slack, and the recorder refuses a
window anchored away from the probe's time.

**Report.** Carry this section, filled in:

```markdown
## Future windows

- **Recommended**: <zone> starting <UTC time> — <one line on why it ranked
  first>
- **Runners-up**: <zone and start per window, or "none">
- **No window**: <zone: status and detail, one line each, or "none">
- The job must start by <UTC time> to finish inside the horizon.
```

**Unattended runs.** A session with no person in it — a scheduled re-check,
an event-triggered run — asks no clarifying questions: take the missing
values from the original request being re-checked or from the capability's
recorded defaults, list every assumption in the report, and propose rather
than apply any change to how the check runs.

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

If configuring fallback Spot instances or diagnosing GPU stockouts, use the Spot advice APIs to check obtainability and preemption risk across target zones.

**VM & GPU Availability Advice**:

```bash
gcloud beta compute advice capacity \
    --provisioning-model=SPOT \
    --instance-selection-machine-types="g2-standard-4,g2-standard-12,n1-standard-4" \
    --target-distribution-shape=ANY \
    --size=1 \
    --region=us-central1 \
    --format="json"
```

**Per-zone follow-up** (when the region-level call above returns fewer than
two zones — `ANY` reports the single best one, and a design needs the
sideways look; run once per remaining zone of the region that supports the
family):

```bash
gcloud beta compute advice capacity \
    --provisioning-model=SPOT \
    --instance-selection-machine-types="a2-highgpu-8g" \
    --target-distribution-shape=ANY \
    --size=4 \
    --region=us-central1 \
    --zones=us-central1-a \
    --format="json"
```

**Flex-Start Availability Advice** (the same probe with
`--provisioning-model=FLEX_START` and the job's run duration):

```bash
gcloud beta compute advice capacity \
    --provisioning-model=FLEX_START \
    --instance-selection-machine-types="a2-highgpu-8g" \
    --target-distribution-shape=ANY \
    --size=4 \
    --region=us-central1 \
    --max-run-duration=12h \
    --format="json"
```

**Preemption Rate and Price History**:

```bash
gcloud beta compute advice capacity-history \
    --provisioning-model=SPOT \
    --machine-type=g2-standard-4 \
    --types=PREEMPTION,PRICE \
    --region=us-central1 \
    --format="json"
```

#### E. Future Reservation Calendar Advice

For a deadline-bound batch job (the **Future windows** section above), probe
each candidate region for its best predicted window. TPU shapes take a
version, chip count, and workload type; VM shapes take a machine type and VM
count (plus `--local-ssd` where the shape has one):

```bash
gcloud beta compute advice calendar-mode \
    --region=us-central1 \
    --tpu-version=V5E --chip-count=256 --workload-type=BATCH \
    --duration-range=min=1d,max=1d \
    --start-time-range=from=<now>,to=<now plus horizon minus job duration> \
    --location-policy=us-central1-a=ALLOW \
    --format=json
```

Compute both ends of `--start-time-range` from the clock at probe time, as
RFC 3339 UTC (`date -u +%Y-%m-%dT%H:%M:%SZ`) — never reuse a timestamp
from an example or an earlier turn; a window that starts in the past
cannot be reserved and the recorder refuses it. `--location-policy`
narrows to the zones the user allowed, and is omitted to consider every
zone in the region.

_MANDATE_: You MUST actually execute the quota check (`gcloud compute regions
describe`), the capacity advice (`gcloud beta compute advice capacity`), and,
where preemption history matters, `gcloud beta compute advice capacity-history`
— for a deadline-bound batch job, `gcloud beta compute advice calendar-mode`
per candidate region — then record each as typed evidence and list the exact
commands you ran in your report. An analysis you did not execute is not
evidence.

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
