# Obtainability journeys: Day-0 cluster design (CUJ1) and Day-2 batch window planning (CUJ3)

**Status:** design of record. Both journeys' behavior is on `main`: the skill, the routing, the
allowlist, the live tests and a nightly eval case for each. What is not on `main` is the evidence
path the skill names — the `record_evidence` and `attach_artifact` tools and the portal projection
that would carry their records to the live graders — so against a stock install the live tests'
evidence- and artifact-scored criteria fail, each naming the missing projection as its
`missingProof`. The Scope table is the boundary.

## In short

Two journeys ask the same underlying question — _will the hardware I want actually be there?_ —
at two different times:

- **CUJ1, Day 0.** "Design me a cluster for 32 A100s in us-central1." The agent checks quota and
  live obtainability separately, names every provisioning path with a verdict, and hands back a
  ComputeClass with fallbacks plus a Node Auto-Provisioning alternative.
- **CUJ3, Day 2.** "Run my 64-node TPU v5e job for 12 hours sometime in the next 48." The agent
  asks the capacity calendar once per candidate region for the window in which that shape is
  predicted obtainable, ranks the windows, recommends an exact UTC start and zone, and hands back
  a Dynamic Workload Scheduler `ProvisioningRequest` paired with a Kueue `LocalQueue` aimed at
  that window.

They share one architecture: one skill that owns the Compute Advice API family for design and
planning requests and the quota-versus-obtainability discipline, a preflight line in each entry skill that routes to it, a
command allowlist that lets its probes run, pinned manifest schemas as the correctness check on a
run that has nothing to dry-run against, typed evidence a grader can read, and per journey one
live critical-user-journey test and one nightly eval case.

## Scope

| Piece                                   | On `main`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | Not on `main`                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The obtainability skill                 | `agents/platform/skills/capacity-obtainability/SKILL.md`: quota (`gcloud compute regions describe`), reservations, `gcloud beta compute advice capacity` for Spot and Flex-Start with a per-zone `--zones` follow-up when a region-level call reports fewer than two zones, `capacity-history`; the quota-versus-obtainability rule; the banned word "guarantee"; a required **Provisioning paths** section; pinned ComputeClass and NAP shapes. Its **Future windows** section: `calendar-mode` once per candidate region, the chips-per-node and one-day-floor facts, the ranking rule, two record types, pinned `ProvisioningRequest` and `LocalQueue` shapes, a required **Future windows** report section, and the rule for unattended runs. | A clarifying step for an under-specified attended request (count, interconnect, Spot tolerance, run duration): the CUJ prompts arrive fully specified, and the skill probes without asking.                                                                                                                                                                                                                                                                                  |
| Routing from the entry skills           | `gke-cluster-creation`, `gke-batch-hpc` and `gke-workload-scaling` each carry a preflight paragraph, appended by `scripts/sync-upstream-skills.py` from `SKILL_FOOTERS`, that loads the skill for a GPU/TPU or large-shape design, batch job, or scale-up. The Platform Agent's `AGENTS.md` routes a free-standing availability question — one that names no design and no job — to the skill directly.                                                                                                                                                                                                                                                                                                                                           | Nothing. No test pins the free-standing route.                                                                                                                                                                                                                                                                                                                                                                                                                               |
| Command allowlist                       | `agents/platform/scripts/command_policy.py` allows the `advice capacity`, `capacity-history` and `calendar-mode` verb paths and every flag the skill's spellings carry, including `--max-run-duration`, `--zones`, and the nine `calendar-mode` flags (`--tpu-version`, `--chip-count`, `--workload-type`, `--vm-count`, `--local-ssd`, `--duration-range`, `--start-time-range`, `--end-time-range`, `--location-policy`). `test_command_policy.py` pins the exact spellings: the per-zone capacity follow-up and three `calendar-mode` forms (TPU; VM with an end-time range; VM with local SSD).                                                                                                                                               | Nothing.                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| Typed evidence and artifacts            | The skill tells the agent to call `record_evidence` once per check and `attach_artifact` once per manifest, with the key shapes spelled out, and, when the tools are absent, to put the same JSON under an `## Evidence` heading in the report and the manifests as fenced YAML. The CUJ helpers (`bench/cuj/utils/interaction.py`) grade `evidence` and `artifacts` on the portal's projected tasks.                                                                                                                                                                                                                                                                                                                                             | The two tools, and a portal projection that carries `evidence`, `artifacts` and `toolEvidenceComplete` on the interaction or its tasks, and `skills` and `loadedSkills` on a task. Neither exists on `main` (#867 tracks the CUJ3 half), so a run produces the fallback and every criterion that reads the projection fails with the gap named in its `missingProof` (`portal task projection omits evidence` in CUJ1's test, `portal projection omits evidence` in CUJ3's). |
| Tests                                   | Live, under `bench/cuj/obtainability/`: `test_01_cluster_design.py` (CUJ1), `test_03_workload_obtainability_planning.py` (CUJ3), `test_04_scheduled_window_recheck.py` and `test_05_reactive_window_replan.py` (the two re-check trigger paths); each names `capacity-obtainability` in `REQUIRED_SKILLS`. Manual by design — they need a real install (`bench/cuj/README.md`). Nightly, in `hack/eval/nightly-cases.txt`: `obtainability-design-quota-vs-capacity` and `obtainability-window-planning-probe`, which grade the worker commands and the report through the eval harness and need no portal projection.                                                                                                                             | Presubmit seats for the two cases, earned on their nightly record (`docs/eval-gate-roster.md`); both are `validated: false` until their first observed runs.                                                                                                                                                                                                                                                                                                                 |
| Batch primitives the answer is built on | `gke-batch-hpc` teaches Kueue (`kueue.x-k8s.io/v1beta1` ClusterQueue and LocalQueue), JobSet, compact placement and Spot for batch; `gke-compute-classes` teaches ComputeClass priorities and the AI/ML priority order (`Reservations -> On-Demand -> DWS FlexStart -> Spot`).                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | Nothing; the Future windows section reuses these shapes.                                                                                                                                                                                                                                                                                                                                                                                                                     |

Out of scope: creating anything. Both journeys are design- and planning-only; the agent's cloud
and Kubernetes grants are read-only for them, and every generated manifest is handed to the user,
never applied.

## The shared architecture

### One skill owns the Advice API family

`capacity-obtainability` is the single place a design or planning request asks Google Cloud
whether hardware is obtainable; the stockout-prevention SOP, its `fleet-audit` collector and the
`gke-stockout-investigator` plugin run the same advice calls at audit and alert time under their
own rules. Four calls, four questions:

| Call                                          | Question it answers                                                               | Journey    |
| --------------------------------------------- | --------------------------------------------------------------------------------- | ---------- |
| `gcloud compute regions describe` (quotas)    | Is this project _allowed_ this much? With reservations, the On-Demand assessment. | CUJ1, CUJ3 |
| `gcloud beta compute advice capacity`         | Is this shape _free right now_, per zone, for Spot and Flex-Start?                | CUJ1       |
| `gcloud beta compute advice capacity-history` | How often has this shape been preempted or unobtainable lately?                   | CUJ1       |
| `gcloud beta compute advice calendar-mode`    | _When_, within a horizon, is this shape predicted obtainable, and where?          | CUJ3       |

The discipline is the same in both sections of the skill: quota and obtainability are separate
questions and are reported separately; On-Demand has no advance obtainability signal and is
assessed from quota and reservations; a probe that fails is a finding in the report, not a silent
gap; the word "guarantee" is banned; every probe is executed against the real API, never answered
from memory.

### Entry skills route to it with a preflight line

Hermes picks the entry skill from the user's words — `gke-cluster-creation` for "design a cluster",
`gke-batch-hpc` for "schedule a training job", `gke-workload-scaling` for "scale this up". Each
carries one paragraph: before recommending capacity, a start time or a zone for a GPU/TPU or
large-shape request, load `capacity-obtainability` and run the diagnostics that apply — the Future
windows section when the request is a deadline-bound job. The paragraph is authored in the sync
script's footer registry (`scripts/sync-upstream-skills.py`, `SKILL_FOOTERS`) and appended to each
skill file by the sync, because these skills are copied from upstream wholesale: an edit made in
the skill file alone lasts until the next run.

A free-standing question — "are A100s obtainable in us-central1 this week?" — names no design and
no job, so no entry skill's preflight fires. One routing bullet in the Platform Agent's `AGENTS.md`
sends it to the skill directly.

### The allowlist decides what the skill can actually run

The agent's shell runs behind `command_policy.py`: a command is allowed only if its verb path
_and_ every flag it carries are known, and an allowlisted verb whose flags are unknown is refused
before the allowlist is consulted. Adding a probe therefore means adding its flags and a test that
pins the exact spelling the skill emits. The allowlist taught this twice: `--max-run-duration` on
the Flex-Start probe, then `calendar-mode`, whose verb path was allowlisted alongside `capacity`
while none of its flags were, so every spelling the skill could emit was refused.

### Pinned schemas are the correctness check

Neither journey has a cluster to validate against: CUJ1 is designing one, and CUJ3's job is not
running yet. So the skill pins the manifest shapes and says a manifest that departs from them is a
finding, not a deliverable. CUJ1 pins `cloud.google.com/v1` ComputeClass (family, not machine type;
On-Demand or reservation first, Spot on the same family second, smaller accelerators as later tiers,
never Spot as the primary) and the NAP shape (node affinity on `cloud.google.com/machine-family`,
`locationPolicy` under `location`). CUJ3 pins the `ProvisioningRequest` and `LocalQueue` described
in its section below.

### Evidence a reviewer can verify

Every probe is recorded as a typed record built from the real command output — `quota_check`;
`advice_service_capacity` with `apiMethod: compute.beta.AdviceService.Capacity`;
`advice_service_workload_obtainability_planning` with `compute.beta.AdviceService.CalendarMode` —
with a fixed key shape, and every manifest is attached as a parsed object under a shared `pairId`.
The skill spells the shapes out, and the live graders read exactly those keys, so the record is a
contract between the two rather than a convention. The tools that carry the records to the portal
are the missing piece: until they land, the skill's fallback (the evidence JSON under an
`## Evidence` heading, the manifests as fenced YAML) keeps the information in front of the human
reader, and each grader that reads the projection fails with the gap named in its `missingProof`.

### One live test and one nightly case per journey

Each journey has a pytest module under `bench/cuj/obtainability/` that talks to the deployed agent
through the admin portal and grades only what came back: a prompt, backend-independent acceptance
criteria, and diagnostic milestones. They run against a real install and no CI job runs them. Each
also has a nightly eval case under `bench/tasks/` that grades the same behavior where the harness
can see it — the worker's commands and the report — so a regression shows in the nightly record
without a portal projection. The banned word is checked there with `report_contains`'s
`forbidden_patterns`, a regular expression that fails on "guarantee" and lets a negated use
("no guarantee") through ([`bench-case-format.md`](bench-case-format.md)).

## CUJ1 — Day-0 cluster design

Persona: a platform engineer designing a cluster for a specific accelerator requirement.

**Flow.** The user asks for a cluster with a concrete accelerator need. `gke-cluster-creation`'s
preflight loads `capacity-obtainability`; the skill checks the exact quota metric (for A100s,
`NVIDIA_A100_GPUS`), lists reservations, probes `advice capacity` for Spot and Flex-Start — a
region-level call with `--target-distribution-shape=ANY` reports its single best zone, so the
per-zone follow-up runs until at least two zones are covered — and consults `capacity-history`
where preemption matters. The answer compares Autopilot and Standard, states quota headroom and
obtainability separately, names all three provisioning paths with a verdict, and attaches a
ComputeClass with fallbacks — calling out that an L4 or T4 tier changes the GPU class and
interconnect — plus the NAP alternative.

**What `test_01_cluster_design.py` grades.** The request preserved (32 A100s, us-central1);
`AdviceService.Capacity` evidence naming that request, with a numeric available quantity, signals
for at least two zones, and both Spot and Flex-Start; a ComputeClass with A2 as the primary
priority and L4/T4 fallbacks; a NAP specification allowing n2, n2d and c2d with
`locationPolicy: ANY`. The skills loading and the absence of any create, apply or pull request are
milestones, which report but do not decide the result.

**What `obtainability-design-quota-vs-capacity` grades.** An 8-A100 design: the quota read and
`advice capacity` ran; the report carries the skill's verbatim sentence "Quota is separate from
live capacity", names On-Demand, Spot and Flex-Start, uses "guarantee" only negated, and carries a
ComputeClass naming `cloud.google.com/v1` or a `machineFamily` priority.

## CUJ3 — Day-2 batch window planning

Persona: an ML researcher or batch-pipeline operator with a deadline and a large accelerator need.

**Flow.** The user asks to run a job of a given size and duration within a horizon.
`gke-batch-hpc`'s preflight loads `capacity-obtainability`; the quota check runs first, because a
window you lack quota for is not a plan; the Future windows section then probes
`advice calendar-mode` once per candidate region, ranks the windows the responses return, and
recommends an exact UTC start time and zone whose run ends inside the horizon. It attaches a
`ProvisioningRequest` sized to the job and a `LocalQueue` that targets it, both under one `pairId`,
and names the runner-up windows and every allowed zone that returned no window, with its status.

**The probe.** `calendar-mode` answers for a _future reservation in calendar mode_: given a shape, a
count, a duration range, a start-time range and an optional per-zone allow/deny policy, it returns
the best window in which that reservation is predicted obtainable in the region, plus a per-zone
status map (`NO_CAPACITY`, `NOT_SUPPORTED`, `CONDITIONS_NOT_MET`) for the rest. Its shape arguments
are one of two groups — TPUs as `--tpu-version` with `--chip-count` and `--workload-type`; VMs as
`--machine-type` with `--vm-count` and optional `--local-ssd` — plus `--region`,
`--duration-range=min=…,max=…`, `--start-time-range=from=…,to=…`, `--end-time-range`, and
`--location-policy=<zone>=ALLOW|DENY`. The skill's Diagnostics E carries the canonical spelling,
one call per candidate region with both ends of the start-time range computed from the clock at
probe time.

Facts about the API shape the call; the skill records them so a builder does not re-derive them:

- **It counts chips, not nodes.** A v5e host carries four chips, so 64 nodes is `--chip-count=256`.
  The report states the arithmetic and the evidence records both numbers, so a reader can check
  the conversion and the grader can check the node count.
- **The minimum reservable window is one day.** A `--duration-range` under `min=1d` returns
  `CONDITIONS_NOT_MET` in every zone. The probe reserves the day; the job's own duration goes in
  the report and in the `ProvisioningRequest`'s `maxRunDurationSeconds`, never in the probe.
- **The deadline bounds the start.** A job of duration D with horizon H cannot start later than
  H − D after now, and that is what `--start-time-range` carries, computed from the clock
  immediately before the call. A 12-hour job with a 48-hour horizon cannot start later than hour 36.

**Ranking.** Every region's recommended window is collected and ordered by earliest start; ties
break toward the zone with the larger quota headroom. Ranks are unique and consecutive from one,
because the grader checks exactly that. The recommended start is the rank-one window's start. If
no allowed zone returns a window, the answer is each zone's status: "no window inside the
horizon" is a complete answer.

**Evidence.** One `advice_service_workload_obtainability_planning` record per region call, whose
`request` carries `region`, `nodeCount`, `chipsPerNode` and the API's `futureResourcesSpecs` body
reconstructed from the flags, and whose `analysis` copies the response's `location`, `startTime`,
`endTime` and `otherLocations`. Then exactly one `workload_obtainability_planning_analysis` record
with `windows` in rank order and `zoneStatuses` for every allowed zone that returned none, so the
analysis covers every allowed zone whether or not it is obtainable.

**Pinned manifests.** The skill carries the two shapes; the fields the grader reads are these. A
Dynamic Workload Scheduler request is `autoscaling.x-k8s.io/v1` `ProvisioningRequest` with
`provisioningClassName: queued-provisioning.gke.io`, one `podSet` whose `count` is the node count
(64, not 256), and `maxRunDurationSeconds` equal to the run (`"43200"` for 12 hours). The queue is
the `kueue.x-k8s.io/v1beta1` `LocalQueue` from `gke-batch-hpc`, naming its backing `ClusterQueue`.
Both sit in the same namespace; the `ProvisioningRequest` carries the recommended zone and start,
and the `LocalQueue` the zone, in `obtainability.kube-agents/` annotations, so the pairing is
visible in the objects as well as the prose; and both are attached with a `machineSpec`, the
shared `pairId`, and a `target` of the rank-one window's region, zone and start.

**What `test_03_workload_obtainability_planning.py` grades.** The request preserved
(64 TPU v5e nodes, 12 hours, 48-hour horizon); a valid `CalendarMode` record for each of the two
allowed regions — us-central1-a and europe-west4-b; us-east4 has no v5e zone, and a contract naming
a zone the API can never recommend is unsatisfiable — with the day-floor durations, the H − D start
range, 256 chips and 64 nodes; windows plus `zoneStatuses` covering both regions; unique
consecutive ranks from one; an exact UTC start and zone whose run ends inside the horizon; a
`ProvisioningRequest` on `queued-provisioning.gke.io` sized to 64 nodes and 43200 seconds; a valid
`LocalQueue` naming its ClusterQueue; both artifacts sharing a `pairId` and targeting the rank-one
window. As in CUJ1, the skills loading and the absence of any apply, submit, create or pull
request are milestones.

**What `obtainability-window-planning-probe` grades.** A 16-node plan, so 64 chips: the
`calendar-mode` command ran with `--tpu-version` v5e and `--chip-count` 64, once for us-central1
and once for europe-west4; the report states the chip conversion, delivers a window verdict (a UTC
time or a per-zone status), uses "guarantee" only negated, and carries the `ProvisioningRequest`
on `queued-provisioning.gke.io` and the `LocalQueue`. The window itself is live capacity weather,
so no check demands one.

### The two re-check paths

A window plan is advice, and [`capability-delivery-vehicle.md`](capability-delivery-vehicle.md)
(R2) says what advice does on the vehicle: it is asked for rather than scheduled, and it gets a
schedule only as a re-check the user opts into — "probe the recommended window again two hours
before it starts" — that fires once and reports into the conversation that asked. An event can
also re-open it: a stockout notification that invalidates the recommended window re-plans against
the remaining allowed zones without asking anyone anything. The skill's rule for unattended runs
covers both: no clarifying questions, missing values taken from the original request, every
assumption listed, and any change to how the check runs proposed rather than applied.
`test_04_scheduled_window_recheck.py` grades the opt-in (the fire time exactly the lead before the
stated start, the destination, a single firing) and `test_05_reactive_window_replan.py` the
unattended re-plan (canonical re-probes, a revised recommendation or an honest no-window,
retargeted manifests, no clarifying question). The schedule mechanism exists
(`cronjob(action='create', deliver='chat')`); delivery into the thread that asked rather than the
relay's per-job session is what R2 adds and the vehicle document's Scope table lists as not built.
