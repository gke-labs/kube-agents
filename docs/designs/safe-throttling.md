# Safe throttling: admission by memory, with refusal as a first-class outcome

## High level summary

kube-agents runs the Planning Agent and every worker it delegates to in one container. A worker is a
process that does one delegated task. A card is the record of one task on the kanban board. The
dispatcher is the part of the gateway that starts a worker for each ready card.

Today the dispatcher limits work by count: at most six workers at a time, by default. No limit
reads memory. When the accepted work needs more memory than the container's request, the node
evicts the pod. The eviction kills every worker and loses the work in flight. This happened three
times in four days on the development install.

Safe throttling has three goals. Never crash a container. Never starve one class of work. Tell a
requester that it is throttled, with a reason and the alternatives.

The first change adds four mechanisms to the dispatcher. It reads the container's own memory use
and starts no worker when memory is short. It stops a worker whose processes grow past a cap. It
refuses a card that waited too long for a slot. Each refusal blocks the card with the new kind
`capacity` and a reason the requester can act on. When the dispatcher cannot read memory, it starts
nothing and logs why. Four settings on the `PlatformAgent` resource tune the mechanisms. Each
setting has a default. Later changes add a budget per class of work, a cost estimate before a
request starts, the same refusal in the credential proxy, and metrics.

## Status

**Status:** design of record, written 2026-11-04 with the first change, which is built: the
container sampler, the pressure guard that fails closed, the headroom cap on a tick's spawns, the
resident cap per worker, the refusal by queue age, the `capacity` block kind, the four
`spec.harness.tuning.admission` fields, and the operator's default memory request for the agent
container raised from 2 GiB to 4 GiB. Not built: the rest of Stage 1 (the front door's reply for
`capacity`, the cost estimate, hysteresis) and Stage 2. The first change edits no SOUL, prompt or
SOP. The tracker entries, the measurements and the sibling changes are listed under References.

## Goals

1. Never crash. No container of the install restarts or is evicted because of the work it accepted,
   at any request rate or fleet size.
2. Never starve. A user's chat question keeps its slot and its memory while scheduled audits or
   triage fan-outs saturate theirs, and the reverse.
3. Feedback, not silence. A request the install cannot take is refused back to the requesting agent
   at once, with a structured reason and the alternatives, so the agent changes strategy instead of
   retrying blindly.

## Non-goals

- Sizing the container beyond its request. The design takes the resolved request as given and
  derives its budget from it; the one sizing change it carries is the operator's default request,
  2 GiB to 4 GiB, because the presubmit install showed the two cannot be separated (below).
- A change to what an agent says or decides. The first change edits no SOUL, prompt or SOP. The
  front door answers a `capacity` block with the rule it already has for a blocked card; the reply
  written for `capacity` is the next change in Stage 1.
- Admission inside the credential proxy. The broker has its slot cap, its child memory budget and
  its resource watermark gate
  ([`credential-proxy-child-memory-budget.md`](credential-proxy-child-memory-budget.md),
  [`docs/credential-isolation-design.md`](../credential-isolation-design.md)); its queue-depth
  refusal in the same reason shape is Stage 2.
- Protecting the node. The kubelet's eviction does that. This design keeps the pod under its request
  so that the eviction never selects it.
- Session pods under `spec.mode: next`. `maxSessions` caps them, each in its own pod with its own
  limit; the dispatcher's workers are subprocesses of the agent container, and they are what this
  design admits.
- CPU, disk and swap.

## Requirements

Functional:

- R1. Admission is memory-aware and real-time: before a worker is spawned, the dispatcher reads the
  container's current memory and every running worker's resident size, and spawns only below a soft
  ceiling.
- R2. Every worker has a resident-size cap; a worker that crosses it is stopped and its card is
  blocked with a `capacity` reason, before the pod reaches its request on Autopilot or its limit
  elsewhere.
- R3. Every request carries a cost estimate before it starts (for fleet work, clusters in scope
  times the per-cluster cost the collector reports); a request over its class budget is refused at
  once, not queued.
- R4. Refusal happens when the queue is long, not when memory is full: a card that has waited past
  its class's bound for a slot is refused with `capacity`.
- R5. Each class of work has a budget of slots and memory: user chat, scheduled audits, triage
  fan-out. A class cannot consume another class's budget.
- R6. A `capacity` refusal reaches the requester as a structured reason, and the front door turns it
  into a reply that names the alternatives (the scheduled report, a batch of at most ten clusters, a
  narrower question). No automatic retry.
- R7. The credential proxy refuses at queue depth with the same reason shape, instead of after a
  60 s wait, and queues per principal.
- R8. The budgets and ceilings are settings on the `PlatformAgent` CR beside `maxInProgress`, with
  defaults from the measurements below; each container reads its own counters.
- R9. The readiness report routes in batches of at most ten clusters per card and prefers the daily
  watch's report when it is fresh; a foreground run cut by the tool-call guard leaves no orphan
  process.

Non-functional:

- N1. Fail closed: if the memory counters are unavailable or stale, the dispatcher refuses rather
  than spawns, and logs why.
- N2. Observable: queue depth, refusals, memory against the ceiling per class, and evictions are
  exported as metrics, and "are you overloaded?" has a real answer in chat.
- N3. No flapping: a decision holds for a full dispatcher tick; hysteresis between admit and refuse.
- N4. Every behaviour is an eval case that was red before the change and green three times after it
  ([`eval_driven_development.md`](../../.agents/rules/eval_driven_development.md)).

Where each lands:

| Stage                  | Requirements                                                                                                    | Shape                                                                        |
| ---------------------- | --------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| Stage 0                | R9, the restart alert                                                                                           | no new machinery; separate changes                                           |
| Stage 1, first change  | R1, R2, R4, R8, N1; the one-tick hold of N3; the `capacity` kind and the reason R6 relies on; the request raise | the dispatcher patch, the operator fields and default request, this document |
| Stage 1, later changes | R3, the front door's reply of R6, hysteresis (N3), the cases of N4                                              | a SOUL rule, the estimate, eval cases                                        |
| Stage 2                | R5, R7, N2, the soak                                                                                            | class budgets, the proxy's refusal, metrics                                  |

## The state before the first change

### The container's counters under GKE Sandbox

The gateway pod's `platform-agent` container runs under GKE Sandbox (gVisor) on Autopilot, with the
operator's default resources as they were: a 2 GiB memory request and an 8 GiB limit (`resolveResources` in
[`manifest_helpers.go`](../../k8s-operator/internal/controller/manifest_helpers.go)). Read inside
the container on the development install on 2026-11-04:

| Counter                                                   | Reads                                                | Means                                                                                        |
| --------------------------------------------------------- | ---------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `/sys/fs/cgroup/memory.max`, `memory.current` (cgroup v2) | absent                                               | the sandbox mounts cgroup v1 only                                                            |
| `/sys/fs/cgroup/memory/memory.usage_in_bytes` (cgroup v1) | 836,599,808 bytes while `kubectl top` showed 787 MiB | within 2% of the kubelet's view: the one counter that tracks the container                   |
| `/sys/fs/cgroup/memory/memory.limit_in_bytes`             | 10.25 GiB                                            | the sandbox's virtual memory, not the container's 8 GiB limit                                |
| `/sys/fs/cgroup/memory/memory.stat`                       | absent                                               | no inactive file cache to subtract there; the working-set subtraction applies on a runc node |
| `/proc/meminfo` `MemTotal`                                | 10.25 GiB                                            | the same virtual memory; `MemAvailable` is that figure less what the sandbox uses            |

### The eviction fires at the request

The pod is Burstable: its limit is above its request. Under node memory pressure the kubelet ranks
the pods whose usage exceeds their request first, and evicts from the top. The agent pod was evicted
three times with usage between 3.1 and 3.5 GiB against its 2 GiB request, each time during one
fleet-wide card. The 8 GiB limit never came into it. On Autopilot the request is the ceiling that
matters, and an eviction kills every worker, loses the cards in flight and restarts the gateway.

### The guard that exists reads the wrong memory

Hermes (pinned v2026.9.14) has a memory guard in the dispatcher's tick. `_tick_spawn_budget()` in
`hermes_cli/kanban_db_dispatch.py` calls `_memory_pressure_level()`, which samples memory through
`gateway.lifecycle_ledger.sample_memory()` (`mem_total_kib` and `mem_available_kib` from
`/proc/meminfo`, `rss_kib` from `/proc/self/status`) and classifies the sample with
`gateway.memory_status.classify_pressure(available_kib, total_kib)`: `elevated` below 15% of total
or 128 MiB available, `critical` below 5% or 64 MiB. `critical` spawns nothing this tick;
`elevated` spawns at most one worker. An empty sample classifies as `unknown`, and `unknown`
imposes no restriction: upstream's choice, so that a host without `/proc` still dispatches.

The image patches that sample once already.
[`apply_cgroup_memory_sample.py`](../../deploy/docker/patches/apply_cgroup_memory_sample.py) adds
the container's cgroup v2 limit and usage when `memory.max` is set, so that on a runc node the guard
reads the container and not the node. Under GKE Sandbox the v2 files are absent, the helper returns
nothing, and the sample is `/proc/meminfo` again: 10.25 GiB of total. At the 3.5 GiB where the pod
was evicted, 6.75 GiB read as available, 66% of total: `ok`. The guard has never fired on the
install it was meant to protect, and on a runc node it measures against the limit where the
eviction fires at the request.

### The limits that exist are counts

- `kanban.max_in_progress`, 6 unless the CR sets it, pinned by the operator in the managed scope.
  The scheduling patch
  ([`apply_kanban_scheduling.py`](../../deploy/docker/patches/apply_kanban_scheduling.py)) holds
  one slot for each class of card, user and background, posts `⏳ Queued` in the thread of a
  top-level user card that waits, and logs a saturated board. The CRD page's
  [Why dispatch is capped by default](../site/src/content/docs/operator/platformagent-crd.md#why-dispatch-is-capped-by-default)
  has the arithmetic behind the six.
- A per-card `max_runtime_seconds`, enforced in the tick by `enforce_max_runtime()`: SIGTERM, a
  short grace, SIGKILL, then a write transaction that ends the run `timed_out` and returns the card
  to its source phase for the next tick to spawn again.
- The credential proxy's slot cap of 8, its child memory budget, and its resource watermark gate:
  one command at a time from 75% of its limit, background callers refused at once with 429 from
  85%, interactive callers kept one slot. A request that has waited 60 s for admission is refused
  with 503.
- Block kinds. `block_task(conn, task_id, reason=..., kind=...)` in `hermes_cli/kanban_db.py`
  accepts `VALID_BLOCK_KINDS = {"dependency", "needs_input", "capability", "transient"}` and writes
  a `blocked` event whose payload carries the kind and the reason. The notifier's blocked wake
  relays the reason to the front door, whose rule for a blocked card
  ([`agents/chat/SOUL.md`](../../agents/chat/SOUL.md), §2 step 5) says what did not happen, why,
  and what would work instead, and ends on a retry question. A worker that spends its retries on
  429s blocks its own card with kind `transient`.

In the dispatcher, none of these reads the container's memory, none refuses a card with a reason
the requester can act on, and none keeps one class of work from taking another class's memory.

## The mechanism, by layer, in the first change

Every mechanism lands in one patch under `deploy/docker/patches/`, beside the scheduling patch,
anchored on the upstream functions named below, and in the operator's rendering of the managed
scope. Every log line the mechanisms write starts `kanban admission:`, so one search reads them all.

### 1. A sample that reads the container

The dispatcher's guard gets a sampler of its own. It reads usage from the first source that answers:

1. `/sys/fs/cgroup/memory/memory.usage_in_bytes`, cgroup v1: what GKE Sandbox exposes, within 2% of
   the kubelet's view.
2. `/sys/fs/cgroup/memory.current`, cgroup v2: a runc node.
3. The sum of `VmRSS` from `/proc/<pid>/status` over the dispatcher's own process tree: the gateway
   and everything it spawned, its workers and their MCP proxies. The sum counts a shared page once
   per process and counts no page cache, and it misses a process outside the tree, so it is the
   floor of the chain, not a peer of the two cgroup files; the readings step under Verification
   holds it to within 10% of the kubelet's view before the fallback is relied on.

From a cgroup counter the sampler subtracts the inactive file cache the same cgroup's `memory.stat`
reports (`total_inactive_file` under v1, `inactive_file` under v2). A usage counter includes the
file cache, which reclaim frees before anything is evicted; the kubelet evicts on the working set,
usage less that cache, so the sampler reads the figure the eviction reads, and a warm cache of
clones and binaries is not pressure. A missing file or key subtracts nothing. The sibling cgroup v2
patch subtracts both file lists and reclaimable slab for upstream's node-level thresholds; the
dispatcher's sampler subtracts exactly what the kubelet does, because the request is where the
kubelet acts.

The sampler hands `classify_pressure` two numbers: `total = memory_budget_mib` and
`available = memory_budget_mib - usage`. The classifier and its thresholds stay as upstream ships
them, so the dispatcher, the dashboard's memory banner and the lifecycle ledger's OOM heuristics
keep one vocabulary. At the default budget, the container's 4,096 MiB request, the tick is
`elevated` above 3,482 MiB used and `critical` above 3,891 MiB; at a 2,048 MiB request, the
default before this change, 1,741 and 1,946 MiB. `elevated` permits one worker; whether one is
admitted is the headroom cap's decision below: at 4 GiB the 614 MiB left is one, and at 2 GiB the
307 MiB left is under one reserve, so none. The classifier's 15% band is the reserve the guard keeps under the request. One setting
carries it; there is no second constant for the reserve, and the reserve moves with the request.

The sample is taken once per tick, 5 s on the gateway's profile (`kanban.dispatch_interval_seconds`), and the tick's spawns are then bounded by the budget's headroom at one worker per 512 MiB (`cap_spawn_budget`, the reserve upstream's memory-derived default cap divides by). An `ok` reading therefore cannot let one tick spawn the whole slot budget past the budget, and a decision holds for a tick by construction: the one-tick half of N3.

`sample_memory()` itself and the cgroup v2 patch stay as they are. The heartbeat, the dashboard and
the OOM heuristics keep reading them; only the dispatcher's guard switches to the new sampler,
because the dispatcher is the reader that needs the number that predicts the eviction. The banner
that describes the sandbox's memory is a smaller problem and a separate change.

### 2. The pressure guard, failing closed

`_tick_spawn_budget()` keeps its shape: `critical` spawns nothing this tick and the cards wait;
`elevated` spawns at most one. What changes is `unknown`. With `fail_closed: true`, the default, a
tick whose sampler read no source spawns nothing and logs
`kanban admission: memory pressure is unknown (<cause>); fail_closed is on, so this tick spawns nothing`,
once per tick. With `fail_closed: false` the tick behaves as upstream's: no restriction.

The reason to close: the dispatcher that cannot see memory and spawns anyway is the dispatcher that
evicted the pod. The chain of sources ends at `/proc/self/status`, readable wherever the dispatcher
runs on Linux, so the closed state is reached by a real fault, a cgroup mounted elsewhere or a
hardened `/proc`, and the log line names it. The setting is the way out on an install where that
fault is permanent and accepted. The cost while closed is bounded: nothing is lost, cards wait, and
the refusal by queue age below tells each requester after two minutes why nothing started.

### 3. The cap on one worker

The pressure guard decides before a spawn and has no hold on a worker that is already running. One
worker reading a readiness report over the whole scope grows past 3 GiB, and nothing the guard does
stops it. The cap is what stops a single runaway in flight.

Each tick, in the reclaim phase after `enforce_max_runtime()`, the dispatcher sums `VmRSS` over
each running worker's pid and its descendants, found through the parent field of `/proc/*/stat`
(a pid is measured only when its process is the run's own worker: after a container restart the
namespace hands a predecessor's pid to a new process while the stale row is still host-local, and
that process is newer than the stale run, not older. The command line decides where it is readable,
because upstream's spawn names the task in it; otherwise a run that started before this dispatcher
process was recorded by a predecessor and its pid names nothing; otherwise a process that started
before the run cannot be its worker. A row the check cannot vouch for is left to the crash sweep)
(gVisor does not expose `/proc/<pid>/task/<tid>/children`, and one walk that reads on both runtimes
beats two), and compares the sum with `worker_rss_cap_mib`. A worker over the cap is stopped the way
a reclaim stops one, through upstream's `_terminate_reclaimed_worker`: SIGTERM, a short grace,
SIGKILL if it stays, and a result that says whether the worker is gone. Then two sweeps, because a
worker's tool subprocesses hold the memory as much as the worker does: the descendants snapshotted
before the SIGTERM, each checked by start time so a reused pid is not mistaken for one, and the live
members of the worker's process group, which is how a child forked into the group during the grace
is caught (the dispatcher starts a worker as a session leader, so the group is its pid, and the ssh
client of a terminal command, started with a plain `Popen`, stays in it; only a child that starts a
session of its own after the snapshot escapes both sweeps, and the pressure guard still counts it).
A worker the ladder could not signal keeps its card and is measured again next
tick, so a card is never blocked under a live worker the dispatcher cannot stop; a worker with a
SIGKILL delivered is treated as gone, whatever the first probe said, so the block is never lost to a
later tick that finds the pid gone. Then, in one write transaction, its card is blocked with `block_task(..., kind="capacity")`
and its run ended as `blocked`. Not `timed_out`: a timed-out card is returned to its source phase
for the next tick to spawn again, into the same wall. The scope is the claims this host made, by the
`claim_lock` prefix, as `enforce_max_runtime()` scopes itself.

The reason reads, for example: `capacity: the worker used 1,612 MiB against the 1,536 MiB cap; the install cannot take this now. Options: wait for the queue to drain, ask for a smaller scope (at most 10 clusters), or use the scheduled report.`

A worker's tree inside the container is its `hermes chat` process, its MCP proxies and their
supervisor; the commands it runs through the terminal tool execute in the shell sandbox pod,
outside this container and this cap. A coordinator waiting on its children is discounted from the
slot count by the scheduling patch but stays resident, so the cap applies to it as to any worker;
its children are workers of their own, spawned by the dispatcher, not descendants of its process.

### 4. The refusal by queue age

A card that waits for a slot costs nothing while it waits, and it starts eventually. What it costs
is the user's time. The success criteria ask for a simple question answered under one minute and
a first acknowledgement within ten seconds
([`kube-agents-success-criteria.md`](../kube-agents-success-criteria.md), SC3 and SC4). A card
still queued at two minutes is past any answer the user is waiting for, and a strategy change, the
scheduled report or a smaller scope, serves the
user better than an answer twenty minutes later. Two minutes is 24 ticks at the 5 s dispatch
interval: 24 chances to spawn before the refusal.

After the ready and review loops, before the tick returns, the dispatcher walks the ready rows it
did not spawn and blocks, with kind `capacity`, each one older than `queue_bound_seconds` whose
reason for waiting was memory (the tick's early return, or the headroom cap on its spawns), an
exhausted slot budget (the loop's break) or the scheduling patch's per-class reservation holding it
back. It never refuses a card it skipped as unassigned, non-spawnable, respawn-guarded or
per-profile capped: those are routing and quota states, and a `capacity` reason would name the
wrong cause and send the user the wrong alternatives; a row the ready loop never reached is asked
the same four questions, the per-profile cap from the running counts. Nor does it refuse a child of
a request already running (a card with a dependency parent in `task_links`, or one a worker filed,
which the `created` event records as `creator_task_id`): a fan-out's per-cluster cards wait for the
coordinator's own slots, the thread is subscribed to the coordinator, and a refusal would tell the
user to narrow a scope they did not exceed. The age is the time since the card last
entered the ready lane: the latest of its creation, the end of its last run, and the event that
returned it to the lane (a promotion, an unblock, an assignment, a reviewer's changes request, a
review reopened, a reclaim). The reason follows the same shape:
`capacity: the card waited 300 s for a worker slot against the 120 s bound; the install cannot take this now. Options: ...`.

A card refused this way is not retried on its own. `kanban_unblock` returns it to `ready` and its
wait starts again. The `⏳ Queued` line of the scheduling patch is the first thing a waiting user
card's thread shows; a card told it is queued in a tick is not refused in that tick, so the refusal
is a later line, at the bound or the first limited tick after it.

### 5. The `capacity` kind and its reason

`"capacity"` joins `VALID_BLOCK_KINDS`. `_route_block` treats it as it treats every kind but
`dependency`: the card goes to `blocked`, the event payload carries the kind, the reason and a
recurrence count, and a second block of the same kind after an unblock reaches
`BLOCK_RECURRENCE_LIMIT` (2) and routes the card to `triage` for a human.

The reason is one sentence of fact and one of options, in the requester's terms: MiB, seconds,
clusters. No process id, file name or setting key, because the front door relays it in its own
voice and must not have to translate it. The notifier's blocked wake carries it to the front door
unchanged, and the front door's existing rule for a blocked card produces a reply from it with no
prompt change: what did not happen, why, what would work instead, a question. The reply written for
`capacity`, which offers no retry and names the three alternatives, is the next change in Stage 1
and carries its own eval case.

### 6. The settings and where they are read

The dispatcher reads four keys under `kanban.admission` through `load_config_readonly()`, as it
reads `kanban.max_in_progress`. The operator pins them in the managed scope: the
`platform-agent-config` ConfigMap's `managed-config.yaml` key, mounted at `/etc/hermes`, rendered
in [`platformagent_manifests.go`](../../k8s-operator/internal/controller/platformagent_manifests.go)
beside `max_in_progress`. The operator always renders the block with all four keys, defaults
applied in Go, so the numbers in force are the operator's. The patch keeps a copy of the three
constants for a key that is missing or malformed, held equal to the operator's by
[`tests/test_kanban_admission_defaults_parity.py`](../../tests/test_kanban_admission_defaults_parity.py).

The CR carries them as `spec.harness.tuning.admission`, type `AdmissionSpec`, four optional
pointers beside `maxInProgress` in `TuningSpec`
([`common_types.go`](../../k8s-operator/api/v1alpha1/common_types.go)):

```yaml
spec:
  harness:
    tuning:
      maxInProgress: 6
      admission:
        memoryBudgetMiB: 2048 # default: the agent container's resolved memory request
        workerRssCapMiB: 1536
        queueBoundSeconds: 120
        failClosed: true
```

### 7. The tick after the first change

1. The reclaim phase: zombies, stale claims, orphans, crashed workers, `enforce_max_runtime()`, the
   per-worker cap, then `recompute_ready`. The cap runs before promotion so that a card it blocks is
   not promoted in the same tick.
2. `_tick_spawn_budget()`: the slot arithmetic, then the pressure level from the container's
   sample. `critical`, or `unknown` with `fail_closed`, returns before the loops.
3. The ready and review loops, unchanged.
4. The pass by queue age over the ready rows that step 2 or the budget in step 3 left waiting.

## Settings and defaults

| Key under `kanban.admission` | CR field            | Default                                                           | Bounds        | Why this value                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| ---------------------------- | ------------------- | ----------------------------------------------------------------- | ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory_budget_mib`          | `memoryBudgetMiB`   | the agent container's resolved memory request in MiB; 4,096 today | 512 to 65,536 | on Autopilot the request is where the eviction fires, and a budget at the limit admits 8 GiB of work into a pod evicted at its request; the operator's default request is 4 GiB from this change, because a `spec.mode: next` gateway idles at about 1.5 GiB of working set and the 2 GiB default admitted no worker on the presubmit install (every tick capped at zero, every card refused after the bound); following the resolved request means a raise of it, through `spec.deployment.resources` or a new operator default, moves the budget with it; the floor is the least value the schema and the operator admit, and a derived budget under it (a request the operator cannot read as one) is clamped to it and logged: at the floor the dispatcher still admits nothing, because every measured container idles above 512 MiB, and refuses every card after the bound, which is what the clamp buys over a silent fall-back to the meminfo guard, a refusal that names its figures; the ceiling is above any node the install runs on |
| `worker_rss_cap_mib`         | `workerRssCapMiB`   | 1,536                                                             | 256 to 65,536 | an idle worker is about 430 MiB and a long poll loop grows past 1 GiB; 1.5 GiB leaves a fleet-sized card room and stops a runaway before it alone carries the pod from its 1.8 GiB idle set past a 4 GiB request; under a 2 GiB request the pressure guard acts first; the floor is below an idle worker on purpose, for a test install, and a value under 430 stops every worker as its proxies start                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `queue_bound_seconds`        | `queueBoundSeconds` | 120                                                               | 10 to 3,600   | two minutes is 24 ticks at the 5 s dispatch interval, and a card still queued then is past the one-minute answer the success criteria set for a simple question, so a strategy change serves the user better; the floor is two ticks, so no card is refused before the dispatcher has tried twice; the ceiling is an hour, past which a refusal tells the user nothing                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `fail_closed`                | `failClosed`        | `true`                                                            |               | a dispatcher that cannot read memory and spawns anyway is the one that evicted the pod; the chain of sources ends at `/proc/self/status`, so the closed state marks a real fault, which the log names; `false` is the way out where the fault is permanent and accepted                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |

## What later stages add

The rest of Stage 1, each with its red-to-green case:

- The front door's reply for a `capacity` block in `agents/chat/SOUL.md`: no retry, the
  alternatives (the scheduled report, a batch of at most ten clusters, a narrower question), and the
  `[next: …]` line for the one the user is likeliest to want.
- The cost estimate (R3): before a request starts, clusters in scope times the per-cluster cost the
  collector reports; a request over its class budget refused at once, not queued. It is the
  mechanism that bounds a cold burst inside one tick (Failure modes, item 3).
- Hysteresis between admit and refuse (N3), beyond the one-tick hold: a band the level has to cross
  on its way back to `ok` before the full spawn budget returns.

Stage 2:

- Class budgets (R5): chat 40%, scheduled 40%, triage 20% of the slots and of the budget, built on
  the user and background slot floor the scheduling patch has; a class cannot consume another's
  budget, and the queue bound is per class.
- The credential proxy's refusal at queue depth (R7): refused when the queue is long, in the
  dispatcher's reason shape, instead of after the 60 s wait; a queue per principal, on the class
  queue the exec-tier design describes
  ([`credential-broker-exec-tier.md`](credential-broker-exec-tier.md), §4).
- Metrics (N2): queue depth, refusals by cause, memory against the budget per class, evictions; and
  an answer to "are you overloaded?" in chat from the same numbers. The site's
  [observability page](../site/src/content/docs/concepts/observability.md) is where the series are
  named.
- The 72-hour soak as a periodic.

Stage 0, with no new machinery: the request raised to 4 GiB, carried by the first change; as
separate changes, the restart and eviction alert in the chart's health rules, on by default; the readiness report routed
in batches of at most ten clusters per card, the daily watch's report preferred when it is fresh;
the foreground run cut by the tool-call guard killed with its process tree (R9).

## Failure modes

1. The counters cannot be read. Nothing starts; the log names the source and the cause once per
   tick; after two minutes each waiting card is refused with a reason that says the install cannot
   take it now. `failClosed: false` restores upstream's behaviour where the fault is accepted.
2. The budget is above the resolved limit, or below the idle set. Above the limit, the kernel's OOM
   kill (at the limit) or the eviction (at the request) comes before the guard. Below the idle set,
   every tick is `critical`, nothing ever spawns, and every card is refused by age with the same
   reason. Both are visible in the log and in every reply; neither is prevented by the schema's
   bounds, which are absolute. Open question 2 asks whether the operator should clamp or warn.
3. A cold burst inside one tick. The sample is taken once per tick; under `ok` the loops could
   otherwise spawn the whole slot budget, six workers at about 430 MiB each from an idle board, on
   one reading that said the pod was under 1,741 MiB. The tick's spawns are therefore bounded by
   the budget's headroom at one worker per 512 MiB (`cap_spawn_budget`): at 800 MiB used of a
   4,096 MiB budget that is six new workers per tick, at 1,500 MiB used five, and the next tick
   reads the result before admitting more. The cost estimate of a later stage refines the reserve per request.
4. Running workers together exceed the budget while each stays under its cap. Six workers at
   1.5 GiB are 9 GiB. The cap bounds one runaway, the pattern of the three evictions; the sum is
   bounded only by `max_in_progress` times the cap until the class budgets land. The guard stops
   new spawns at `elevated`, so the sum grows only through workers already running.
5. The RSS walk undercounts or overcounts. A descendant whose parent exited is reparented to pid 1
   and leaves the tree: an undercount, and the pressure guard, which reads the whole container,
   still holds. A page shared between a worker's processes is counted once per process: an
   overcount, and the worker is stopped a little early.
6. gVisor does not expose `/proc/<pid>/task/<tid>/children`, so the walk reads the parent field of
   `/proc/*/stat`, on both runtimes. Where `/proc` itself is unreadable the tree is the worker pid
   alone, about half of the worker's footprint (`hermes chat` is 207 of the 430 MiB), and the sum is
   a floor; the pressure guard, which reads the whole container, still holds.
7. The refused card comes back. The front door's current rule ends on a retry question; a user who
   says yes refiles the request, which waits two minutes and is refused again. A card unblocked and
   refused again, rather than refiled, reaches the recurrence limit and routes to `triage`. Both
   are the state until the `capacity` reply rule lands, and both tell the user why each time.
8. The front door's reply. The first change relies on the generic blocked-card rule; the reason text carries the
   options so that the reply names them; the rule itself is not written for `capacity` until the
   next change.
9. The dashboard's memory banner. It keeps reading `/proc/meminfo` through `sample_memory()` and
   describes the sandbox's 10.25 GiB, not the container; the banner is wrong today and stays wrong
   after the first change (open question 9).

## Verification

### What the first change carries

Tests, in the sense of [`AGENTS.md`](../../AGENTS.md) "Where Tests Go": no model in the loop, run
on every pull request.

- Beside the patch: the sampler against fixture directories for cgroup v1, cgroup v2 and the
  `/proc` sum, in that order of preference, with and without a `memory.stat` to subtract; the
  classification arithmetic at 2,048 and 4,096 MiB; the headroom cap; the RSS walk over a fixture
  `/proc` tree with a reparented child, a reused pid and a child left in the process group; a
  worker the ladder cannot signal, one still seen by the probe after a delivered SIGKILL, and one
  that dies between the poll and the SIGKILL; the exclusions of the pass by queue age and its clock over every
  lane-entry event; `fail_closed` on and off; the reason text's shape; the `capacity` kind through
  `block_task` and `_route_block`; and the verifier's drive of the patched dispatcher over a fixture
  tree, run again against the image's tree in the build.
- In the operator: the four keys always rendered with their defaults, the budget following a CR
  `resources` override, the CRD's bounds, the chart's CRD copy in step (`make chart-check`), and
  the patch's copies of the defaults held to the operator's.
- Under the eval rule
  ([`eval_driven_development.md`](../../.agents/rules/eval_driven_development.md)) the first change
  is a deterministic guard: no model call is in its path, and the text it emits is the reason,
  which the front door reads under its existing blocked-card rule as it reads a `transient`
  block's. The unit tests cover each input class it acts on and the pass-through. The cases with a
  model in the loop are item 3 below; they land with the `capacity` reply rule, the first change to
  what an agent says, and are graded against a worker the case can grow (open question 5).

### The plan, from the requirements

1. Offline: the admission decision is a pure function of the slot counts, the sample, the budget,
   the worker sizes and the card ages. A simulator drives it with random arrivals, random memory
   curves, cost estimates off by 3× and missing counters, and checks the invariants thousands of
   times: usage never above the budget by more than one worker's footprint, a bounded wait per
   class, no decision reversed inside a tick, no class starved. It lives beside the patch's tests
   and runs on every pull request.
2. Readings: a planted worker allocating at a known rate, compared with `kubectl top` and the
   kubelet's view under gVisor; the fallback per-pid sum must track within 10%, or the controller
   fails closed rather than rely on it.
3. Fault injection, each a nightly eval case: one worker growing without bound; a burst of three
   fleet-wide requests over a 49-cluster scope; a cost estimate wrong by 3×; counters frozen; a user
   question every minute during a saturated sweep; a pod restarted by hand mid-sweep. Pass: zero
   restarts or evictions, refusals within two minutes with the hint, the chat question inside the
   success criteria's bands (a simple question under one minute, the acknowledgement within ten
   seconds), one report process per request. The cases
   register in `hack/eval/nightly-cases.txt` under a domain row of their own in
   [`domains.yaml`](domains.yaml); the existing `capacity` domain grades node pools, not the
   install.
4. Soak: 72 hours with the scale runner's daily work at 40 clusters, the cron audits on schedule and
   a scripted chat question every five minutes; zero restarts, chat p95 in band, every refusal
   explained.
5. Keeping it true: the cases on the nightly roster, the soak as a periodic, the restart alert
   tested by killing a pod, two weeks of metrics on the maintainer installs.

## Open questions

1. Settled in the first change: the age clock of the pass by queue age is the latest of the card's
   creation, its last run's end and its last lane-entry event (`promoted`, `promoted_manual`,
   `unblocked`, `assigned`, `changes_requested`, `review_reopened`, `reclaimed`), the events the
   pinned release writes when a card returns to `ready` without a run.
2. A setting outside the sane range relative to the resolved resources (a budget above the limit,
   a cap above the budget): clamp in the operator, warn in the CR's status, or accept and log in the
   dispatcher. The bounds in the schema are absolute and cannot express the relation.
3. Settled in the first change: a tick's spawns under `ok` are bounded by the headroom divided by 512 MiB
   (upstream's `MEMORY_GUARD_MB_PER_WORKER`, the footprint its derived cap assumes per worker).
   Whether the reserve should follow the measured per-worker size instead of a constant is for
   the cost estimate.
4. Whether a reserve setting is needed beside the budget. The classifier's 15% band is the reserve
   this design uses: 307 MiB at a 2 GiB request, 614 MiB at 4 GiB, against the 512 MiB first
   proposed.
5. How the fault-injection cases grow a worker inside the container: the terminal tool runs in the
   sandbox pod, so an allocator script grows the wrong pod. A worker that reads large outputs into
   its own context is the natural allocator; a bench-only hook is the alternative.
6. Whether a `capacity` re-block should count toward `BLOCK_RECURRENCE_LIMIT` and route to
   `triage`, as every kind but `dependency` does, or be exempt: a human cannot add capacity from
   the triage column.
7. The coordinator's residency: discounted from the slot count, counted by the budget. Whether the
   class budgets should charge it to the class of its children.
8. Settled in the first change: the walk reads the parent field of `/proc/*/stat` on both
   runtimes, and the process-group pass catches what that walk misses after a reparent.
9. The dashboard's memory banner and the OOM heuristics still read the sandbox's `/proc/meminfo`.
   Switch them to the same sampler, or leave them.

## References

- Trackers: #4760 (safe-throttling: the requirements and the stages); #4354 (the gap, graded
  Blocking, and the measurements); #3753 (the sizing data and the scale runner); #4758 (the memory
  request raise and the restart alert); #3254 (the cgroup v2 sample patch); #4729 (the credential
  broker's admission control, the design Stage 2's R7 joins).
- Upstream Hermes v2026.9.14: `hermes_cli/kanban_db_dispatch.py` (`_tick_spawn_budget`,
  `_memory_pressure_level`, `_system_memory_sample`, `enforce_max_runtime`, `_run_reclaim_phase`,
  `_dispatch_once_locked`); `hermes_cli/kanban_db.py` (`VALID_BLOCK_KINDS`, `block_task`,
  `_route_block`, `BLOCK_RECURRENCE_LIMIT`); `gateway/memory_status.py` (`classify_pressure`);
  `gateway/lifecycle_ledger.py` (`sample_memory`); `hermes_cli/config.py`
  (`load_config_readonly`).
- This repository:
  [`apply_cgroup_memory_sample.py`](../../deploy/docker/patches/apply_cgroup_memory_sample.py),
  [`apply_kanban_scheduling.py`](../../deploy/docker/patches/apply_kanban_scheduling.py) and
  [`kanban_priority.py`](../../deploy/docker/patches/kanban_priority.py);
  [`platformagent_manifests.go`](../../k8s-operator/internal/controller/platformagent_manifests.go)
  and [`manifest_helpers.go`](../../k8s-operator/internal/controller/manifest_helpers.go);
  [`common_types.go`](../../k8s-operator/api/v1alpha1/common_types.go) (`TuningSpec`);
  [`agents/chat/SOUL.md`](../../agents/chat/SOUL.md);
  [`credential_proxy.py`](../../agents/platform/scripts/credential_proxy.py).
- Documents: [`platformagent-crd.md`](../site/src/content/docs/operator/platformagent-crd.md)
  (`spec.harness.tuning`, why dispatch is capped by default);
  [`credential-proxy-child-memory-budget.md`](credential-proxy-child-memory-budget.md);
  [`credential-broker-exec-tier.md`](credential-broker-exec-tier.md);
  [`docs/credential-isolation-design.md`](../credential-isolation-design.md);
  [`kube-agents-success-criteria.md`](../kube-agents-success-criteria.md);
  [`bench-case-format.md`](bench-case-format.md);
  [`eval_driven_development.md`](../../.agents/rules/eval_driven_development.md).
