# Obtainability journeys: Day-0 cluster design (CUJ1) and Day-2 batch window planning (CUJ3)

**Status:** design. CUJ1's behavior is on `main`; its evidence plumbing and all of CUJ3's behavior
are not. The Scope table is the boundary.

## In short

Two journeys ask the same underlying question — _will the hardware I want actually be there?_ —
at two different times:

- **CUJ1, Day 0.** "Design me a cluster for 32 A100s in us-central1." The agent checks quota and
  live obtainability separately, names every provisioning path with a verdict, and hands back a
  ComputeClass with fallbacks plus a Node Auto-Provisioning alternative.
- **CUJ3, Day 2.** "Run my 64-node TPU v5e job for 12 hours sometime in the next 48." The agent
  asks the capacity calendar for predicted windows per zone, ranks them, recommends an exact UTC
  start and zone, and hands back a Dynamic Workload Scheduler `ProvisioningRequest` paired with a
  Kueue `LocalQueue` aimed at that window.

They share one architecture: one skill that owns the Compute Advice API family and the
quota-versus-obtainability discipline, a preflight line in each entry skill that routes to it, a
command allowlist that lets its probes run, pinned manifest schemas as the correctness check on a
run that has nothing to dry-run against, and a live critical-user-journey test per journey.

## Scope

| Piece                                   | Already on `main`                                                                                                                                                                                                                                                                                                                                                       | Still to build                                                                                                                                                                                                                                                                                      |
| --------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The obtainability skill                 | `agents/platform/skills/capacity-obtainability/SKILL.md`: quota (`gcloud compute regions describe`), reservations, `gcloud beta compute advice capacity` for Spot and Flex-Start across zones, `capacity-history`; the quota-versus-obtainability rule; a banned-guarantee rule; a required **Provisioning paths** report section; pinned ComputeClass and NAP schemas. | A **Future windows** section for CUJ3: the `calendar-mode` probe per candidate region, window ranking, the start-window recommendation, and pinned `ProvisioningRequest` and `LocalQueue` schemas.                                                                                                  |
| Routing from the entry skills           | `gke-cluster-creation` carries a preflight paragraph (injected as a sync-script footer) that loads the skill for any GPU/TPU or large-shape design.                                                                                                                                                                                                                     | The same preflight in `gke-batch-hpc` and `gke-workload-scaling` for a GPU/TPU batch job with a deadline; a routing line in the Platform Agent's `AGENTS.md` for a free-standing availability question that names no design or job.                                                                 |
| Command allowlist                       | `agents/platform/scripts/command_policy.py` allows `advice capacity`, `capacity-history` and `calendar-mode`; the flags `capacity` needs, including `--max-run-duration`.                                                                                                                                                                                               | The flags `calendar-mode` takes and the allowlist does not name: `--tpu-version`, `--chip-count`, `--workload-type`, `--vm-count`, `--local-ssd`, `--duration-range`, `--start-time-range`, `--end-time-range`, `--location-policy`. An allowlisted command whose flags are unknown is unreachable. |
| Typed evidence and artifacts            | The skill tells the agent to call `record_evidence` and `attach_artifact`, and to fall back to an `## Evidence` section and fenced YAML when it cannot. The CUJ helpers (`bench/cuj/utils/interaction.py`) grade `evidence` and `artifacts` on the portal's projected tasks.                                                                                            | The two tools and the projection that carries their records — neither is on `main` (#804 tracks it). Until they land, the fallback is what a run produces and the evidence-based criteria of both journeys cannot pass against a `main` install.                                                    |
| CUJ tests                               | `bench/cuj/obtainability/test_01_cluster_design.py` (CUJ1) and `test_03_workload_obtainability_planning.py` (CUJ3), with prompts, acceptance criteria and milestones; manual by design — they need a real install (see `bench/cuj/README.md`).                                                                                                                          | CUJ3's `REQUIRED_SKILLS` names `gke-batch-hpc` and `gke-workload-scaling` only; it has to name the obtainability skill once the routing exists. Its "64 TPU v5e nodes" needs the chips-per-host rule below before `--chip-count` can be checked.                                                    |
| Batch primitives the answer is built on | `gke-batch-hpc` teaches Kueue (`kueue.x-k8s.io/v1beta1` ClusterQueue and LocalQueue), JobSet, compact placement and Spot for batch; `gke-compute-classes` teaches ComputeClass priorities and Flex-Start provisioning.                                                                                                                                                  | Nothing new there; the CUJ3 section reuses these shapes.                                                                                                                                                                                                                                            |

Out of scope: creating anything. Both journeys are design- and planning-only; the agent's cloud
and Kubernetes grants are read-only for them, and every generated manifest is handed to the user,
never applied.

## The shared architecture

### One skill owns the Advice API family

`capacity-obtainability` is the single place that knows how to ask Google Cloud whether hardware
is obtainable. Four calls, four questions:

| Call                                          | Question it answers                                                      | Journey    |
| --------------------------------------------- | ------------------------------------------------------------------------ | ---------- |
| `gcloud compute regions describe` (quotas)    | Is this project _allowed_ this much? On-Demand's only advance signal.    | CUJ1, CUJ3 |
| `gcloud beta compute advice capacity`         | Is this shape _free right now_, per zone, for Spot and Flex-Start?       | CUJ1       |
| `gcloud beta compute advice capacity-history` | How often has this shape been preempted or unobtainable lately?          | CUJ1       |
| `gcloud beta compute advice calendar-mode`    | _When_, within a horizon, is this shape predicted obtainable, and where? | CUJ3       |

The discipline the skill already states carries over unchanged to CUJ3: quota and obtainability are
separate questions and are reported separately; On-Demand has no advance obtainability signal and
is assessed from quota and reservations; a probe that fails is a finding in the report, not a
silent gap; the word "guaranteed" is banned; every probe is executed against the real API, never
answered from memory.

### Entry skills route to it with a preflight line

Hermes picks the entry skill from the user's words — `gke-cluster-creation` for "design a cluster",
`gke-batch-hpc` for "schedule a training job". Each entry skill carries one paragraph: before
recommending capacity or a start time for a GPU/TPU or large-shape request, load
`capacity-obtainability` and run the diagnostics that apply. The paragraph lives in the sync
script's footer registry (`scripts/sync-upstream-skills.py`, `SKILL_FOOTERS`), not in the skill
file, because these skills are synced from upstream and an in-place edit would be overwritten;
`gke-cluster-creation` already has its footer, `gke-batch-hpc` and `gke-workload-scaling` get the
same shape.

A free-standing question — "are A100s obtainable in us-central1 this week?" — names no design and
no job, so no entry skill's preflight fires; it reaches the skill only if Hermes matches the skill's
own description. One routing bullet in the Platform Agent's `AGENTS.md` ("a question about whether
GPUs, TPUs or large shapes are available → load `capacity-obtainability`") closes that, and a short
CUJ prompt should pin it.

### The allowlist decides what the skill can actually run

The agent's shell runs behind `command_policy.py`: a command is allowed only if its verb path
_and_ every flag it carries are known. Adding a probe therefore means adding its flags, and a test
that pins the exact spelling the skill emits — the lesson `--max-run-duration` taught CUJ1, where
the command was allowlisted and still unreachable.

### Pinned schemas are the correctness check

Neither journey has a cluster to validate against: CUJ1 is designing one, and CUJ3's job is not
running yet. So the skill pins the manifest shapes and says "a manifest that departs from them is a
finding, not a deliverable". CUJ1 pins `cloud.google.com/v1` ComputeClass (family, not machine type;
On-Demand or reservation first, Spot on the same family second, smaller accelerators as later tiers,
never Spot as the primary) and the NAP shape (node affinity on `cloud.google.com/machine-family`,
`locationPolicy` under `location`). CUJ3 pins the two shapes in its section below.

### Evidence a reviewer can verify

Every probe is recorded as a typed record built from the real command output — `quota_check`,
`advice_service_capacity` with `api_method: compute.beta.AdviceService.Capacity` and a fixed key
shape — and every manifest is attached as a parsed object under a shared `pair_id`. That is what the
CUJ graders read. The tools that carry those records to the portal are the open half of #804; until
they land the skill's fallback (an `## Evidence` heading and fenced YAML in the report) keeps the
information in front of the human reader, and the graders report the criteria as unmet rather than
guessing.

### One live test per journey

Each journey is a pytest module under `bench/cuj/obtainability/` that talks to the deployed agent
through the admin portal and grades only what came back: a prompt, backend-independent acceptance
criteria, and diagnostic milestones. They run against a real install and no CI job runs them.

## CUJ1 — Day-0 cluster design

Persona: a platform engineer designing a cluster for a specific accelerator requirement.

**Flow.** The user asks for a cluster with a concrete accelerator need. `gke-cluster-creation`'s
preflight loads `capacity-obtainability`; the skill checks the exact quota metric (for A100s,
`NVIDIA_A100_GPUS`), lists reservations, probes `advice capacity` for Spot and Flex-Start in at
least two zones, and consults `capacity-history` where preemption matters. The answer compares
Autopilot and Standard, states quota headroom and obtainability separately, names all three
provisioning paths with a verdict, and attaches a ComputeClass with fallbacks — calling out that an
L4 or T4 tier changes the GPU class and interconnect — plus the NAP alternative.

**What `test_01_cluster_design.py` grades.** The request preserved (32 A100s, us-central1); the
three skills loaded; `AdviceService.Capacity` evidence naming that request; a ComputeClass with A2
as the primary priority and L4/T4 fallbacks; a NAP specification allowing n2, n2d and c2d with
`locationPolicy: ANY`; no create, apply, or pull request.

**Open items.** The evidence plumbing above. The clarifying step: the CUJ prompt arrives fully
specified, and the skill has no step that asks for what is missing (count, interconnect, tolerance
for Spot, run duration) before probing; a vaguer real conversation needs one.

## CUJ3 — Day-2 batch window planning

Persona: an ML researcher or batch-pipeline operator with a deadline and a large accelerator need.

**Flow.** The user asks to run a job of a given size and duration within a horizon.
`gke-batch-hpc`'s preflight loads `capacity-obtainability`; its Future windows section probes
`advice calendar-mode` once per candidate region with the shape, count, duration and horizon, ranks
the windows the responses return, and recommends an exact UTC start time and zone whose run ends
inside the horizon. It then attaches a `ProvisioningRequest` sized to the job and a `LocalQueue`
that targets it, both under one `pair_id`, and names the runner-up windows.

**The probe.** `calendar-mode` answers for a _future reservation in calendar mode_: given a shape, a
count, a duration range, a start-time range and an optional per-zone allow/deny policy, it returns
the windows in which that reservation is predicted obtainable. Its shape arguments are one of two
groups — TPUs as `--tpu-version` (`V5E`, `V5P`, `V6E`, `TPU7X`) with `--chip-count` and
`--workload-type` (`BATCH` or `SERVING`); VMs as `--machine-type` with `--vm-count` and optional
`--local-ssd` — plus `--region`, `--duration-range=min=…,max=…`, `--start-time-range=from=…,to=…`,
`--end-time-range`, and `--location-policy=<zone>=allow|deny`. For the CUJ3 prompt, one call per
region:

```
gcloud beta compute advice calendar-mode --region=us-central1 \
  --tpu-version=V5E --chip-count=<chips> --workload-type=BATCH \
  --duration-range=min=12h,max=12h \
  --start-time-range=from=<now>,to=<now+36h> \
  --location-policy=us-central1-a=allow
```

**Units.** The API counts chips; the user and the CUJ count nodes. The skill states the conversion
it applied (a v5e host carries four chips, so 64 nodes is 256 chips) and records both numbers in the
evidence, so a reader can check the arithmetic and the grader can check the node count.

**Ranking.** Windows are ordered by the response's own obtainability signal first, then by earliest
start; ties break toward the zone with the larger quota headroom. Ranks are unique and consecutive
from one, because the grader checks exactly that. The recommended start is the rank-one window's
start, and the recommendation says out loud that the run must finish inside the horizon (a 12-hour
job with a 48-hour horizon cannot start later than hour 36).

**Pinned manifests.** A Dynamic Workload Scheduler request is `autoscaling.x-k8s.io/v1`
`ProvisioningRequest` with `provisioningClassName: queued-provisioning.gke.io`, one `podSet` whose
`count` is the node count and whose pod template requests the TPU topology, and
`maxRunDurationSeconds` equal to the run (43200 for 12 hours). The queue is the
`kueue.x-k8s.io/v1beta1` `LocalQueue` from `gke-batch-hpc`, naming its backing `ClusterQueue`. Both
carry the recommended region, zone and window in their annotations so the pairing is visible in the
objects, not only in the prose.

```yaml
apiVersion: autoscaling.x-k8s.io/v1
kind: ProvisioningRequest
metadata:
  name: <job>-window
  annotations:
    obtainability.kube-agents/zone: us-east4-a
    obtainability.kube-agents/start: "2026-09-16T22:00:00Z"
spec:
  provisioningClassName: queued-provisioning.gke.io
  parameters:
    maxRunDurationSeconds: "43200"
  podSets:
    - count: 64
      podTemplateRef:
        name: <job>-pod-template
---
apiVersion: kueue.x-k8s.io/v1beta1
kind: LocalQueue
metadata:
  name: <job>-queue
  namespace: <namespace>
  annotations:
    obtainability.kube-agents/zone: us-east4-a
spec:
  clusterQueue: <cluster-queue>
```

**What `test_03_workload_obtainability_planning.py` grades.** The request preserved (64 TPU v5e
nodes, 12 hours, 48-hour horizon); `AdviceService.CalendarMode` calls with the shape, count, allowed
zones, duration and horizon; windows in at least two regions; unique consecutive ranks from one; an
exact UTC start and zone whose run ends inside the horizon; a `ProvisioningRequest` on
`queued-provisioning.gke.io` sized to 64 nodes and 43200 seconds; a valid `LocalQueue` naming its
ClusterQueue; both artifacts sharing a `pairId` and targeting the rank-one window; no apply, submit,
create, or pull request.

**Open items.** The skill section, the two preflight footers, the nine allowlist flags and their
test, `REQUIRED_SKILLS` in the test, and the same evidence plumbing CUJ1 waits on. One design
choice to settle before building: whether the recommendation should also schedule a re-check
shortly before the window (a capability on the
capability delivery vehicle, where the horizon, allowed zones and ranking rule become tunable
criteria the operator can change through the agent) or stay a one-shot answer as the CUJ is written.

## Build order

1. The skill's Future windows section, with the pinned shapes and the chips-per-host rule.
2. The allowlist flags and a test pinning the exact `calendar-mode` spelling the skill emits.
3. The `gke-batch-hpc` and `gke-workload-scaling` footers, and the `AGENTS.md` routing bullet for
   free-standing availability questions.
4. `REQUIRED_SKILLS` in `test_03`, plus a CUJ prompt for the free-standing question.
5. A live run of both CUJs against a real install once #804's evidence plumbing lands; until then a
   run proves the behavior from the transcript and the report's fallback sections, and the
   evidence criteria stay unmet by construction.
