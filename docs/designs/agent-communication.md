# Agent Communication Design — kube-agents

> **STATUS — design of record; not yet implemented.** The file-based handover channel described as the "primary channel" below is a proposal: no code in this repository implements it today. The **kanban delegation** design (optional task delegation) builds on the Hermes kanban board, which does exist. Treat this document as the reference design for both, not as a description of shipped behaviour.

**Status:** Draft for review
**Scope:** How the Platform Agent and per-cluster subagents exchange information.
**Primary topic:** structured, file-based handover. **Secondary:** optional task delegation via the Hermes kanban board.

---

## 1. Summary

The platform is a **decoupled, persona-based** system:

- **Platform Agent** — one Hermes profile (the `platform` profile). Fleet-wide synthesis. It is not the chat front door: the `default` profile is the Chat Agent, which owns chat ingress and delegates to this one (§6.1).
- **Cluster subagent** — one Hermes profile **per managed cluster**, co-located in the same pod for the MVP. Runs its own cron for periodic local scans, and processes delegated tasks.

Two communication channels, with different shapes and different reliability requirements:

| Channel                           | Direction          | Shape                                   | Mechanism                                                            |
| --------------------------------- | ------------------ | --------------------------------------- | -------------------------------------------------------------------- |
| **Structured handover (primary)** | cluster → platform | continuous status (latest-wins, typed)  | files under `/opt/data/fleet/…`, written via a `write_handover` tool |
| **Task delegation (optional)**    | platform → cluster | discrete work items (lifecycle/fan-out) | Hermes kanban board                                                  |

**Guiding principles**

1. **No agent-to-agent prompting.** No agent blocks waiting on another. They coordinate through durable state (files) and, when delegating, through the kanban board. This avoids the delegation/loop antipatterns.

   > **Known gap, recorded rather than claimed.** The kanban channel _does_ pass one agent's model output into another's context: a `kanban_create` body becomes the worker's pre-built context (§3.2), and a fan-in card's context carries every parent's structured `metadata`. That is precisely what the **non-authoritative** property in [02](../architecture/02-agent-personas.md) §2.3 exists to control, and nothing in §3 or §5 yet treats a card body as untrusted input to the worker. This channel has **not** been assessed against the four-property test. It should be, before anything else is built on it.

2. **Constrain writes, free reads.** Writers use a schema-enforcing tool; readers use ordinary file tools. LLMs are reliable at reading files and less reliable at bespoke interfaces, so only the _write_ path is a tool.
3. **Co-located now, migrateable later.** For the MVP all profiles share one pod and one PVC. The design keeps a single seam (the write helper) where a future cross-pod transport would slot in without changing the reader contract.

---

## 2. Structured file-based handover (primary channel)

### 2.1 Model

Cluster subagents continuously produce structured **status records** (health, utilization, upgrade-readiness, …). The platform agent consumes them to reason about the fleet. This is a classic producer/consumer blackboard:

- **Producers** (cluster subagents) write one file per `(cluster, record-type)`, latest-wins.
- **Consumer** (platform agent) reads those files whenever it needs fleet state — no request/response, no prompting.

### 2.2 Location

```
/opt/data/fleet/clusters/<cluster>/<location>/<type>.json
```

`/opt/data/fleet` is a **fixed absolute path**, shared by every agent in the pod.

### 2.3 Record envelope

Every file is a single JSON object with a common envelope and a typed `payload`:

```json
{
  "schema_version": 1,
  "cluster": "prod",
  "location": "us-central1",
  "type": "health",
  "generated_at": "2026-07-21T14:03:11Z",
  "expires_at": "2026-07-21T14:18:11Z",
  "payload": {}
}
```

| Field            | Meaning                                                                                                                                    |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `schema_version` | Envelope/schema version for forward compatibility.                                                                                         |
| `cluster`        | The cluster this record describes. Set from the writer's **profile identity**, never a caller argument.                                    |
| `location`       | The cluster location (zonal/regional). Set from the writer's **profile identity**, never a caller argument.                                |
| `type`           | Record type (`health`, `utilization`, `upgrade_readiness`, `drift`, `inventory`, …). Determines the `payload` schema.                      |
| `generated_at`   | Write timestamp (UTC ISO-8601).                                                                                                            |
| `expires_at`     | Staleness horizon. A reader treats a record past `expires_at` as **stale** — this is how a dead subagent's data is detectably out of date. |
| `payload`        | Typed body (see §2.6).                                                                                                                     |

### 2.4 The `write_handover` tool

All writes funnel through a single tool (registered only for cluster-subagent profiles):

- **Name:** `write_handover`
- **Toolset:** `handover` (gated via `enabled_toolsets`).
- **Arguments:**
  - `type` (enum: `health | utilization | upgrade_readiness | drift | inventory`)
  - `payload` (object — the typed body)
  - `ttl_seconds` (optional int; default per-type, used to compute `expires_at`)
- **Handler behavior:**
  1. Derive `cluster` and `location` from the **profile identity** (bound once at `register()` via `ctx.profile_name` / config), so they are **not** an argument. A subagent therefore _cannot_ write another cluster's record.
  2. Build the envelope (`generated_at = now`, `expires_at = now + ttl`).
  3. **Atomic write** (temp file + `fsync` + `os.replace`) to `/opt/data/fleet/clusters/<cluster>/<location>/<type>.json`. Atomicity is load-bearing: it guarantees the platform's plain reads never observe a torn/half-written file. The temp file **must be created in the destination directory**, not in `/tmp`: `os.replace` is only atomic within a single filesystem, and `/opt/data` is a PVC while `/tmp` is usually a separate `tmpfs` — renaming across that boundary fails with `EXDEV` (invalid cross-device link).
  4. Return a short JSON result string.

**One shared write helper.** The tool handler and any deterministic `no_agent` cron scripts call the **same** small helper:

```python
handover.write(cluster, location, record_type, payload, ttl_seconds=None)
```

The parameter is `record_type`, not `type`, so the helper does not shadow the Python built-in. The tool argument and the envelope field stay named `type` — those are the agent-facing and on-disk contracts; only the Python signature is renamed.

This single-sources the envelope, path resolution, and atomicity, so the tool path and the script path can never drift. It is a concrete helper — **not** a generic pluggable interface (that abstraction is intentionally omitted while co-located; see §2.8).

**Writer rule:** cluster subagents write status **only** via `write_handover` (or the helper). They must not `write_file` these paths directly — that would bypass the envelope and break the reader contract. State this rule in the cluster `SOUL.md`.

### 2.5 Reading (platform side)

The platform agent reads with **ordinary file tools** (`list_files`, `read_file`). No custom read tool is needed. Its `SOUL.md` / skill documents three facts:

1. The path: `/opt/data/fleet/clusters/<cluster>/<location>/<type>.json`.
2. The envelope shape (so it parses `payload` and reads `generated_at`).
3. The staleness rule: **treat any record whose `expires_at` is in the past as stale** and do not act on it as current truth.

Because writes are atomic, concurrent reads are always of a complete file.

### 2.6 Record types and example payloads

The MVP starts with `health` and `utilization`; the others are added as their CUJs land. All examples show only the `payload` (the envelope wraps them).

**`health`** — SRE health snapshot.

```json
{
  "overall": "degraded",
  "node_ready_ratio": "11/12",
  "pods_crashlooping": 3,
  "pods_oomkilled_1h": 2,
  "pods_pending": 0,
  "apiserver_p99_ms": 180,
  "failing_workloads": [
    {
      "namespace": "payments",
      "kind": "Deployment",
      "name": "ledger",
      "reason": "CrashLoopBackOff"
    },
    {
      "namespace": "search",
      "kind": "StatefulSet",
      "name": "indexer",
      "reason": "OOMKilled"
    }
  ],
  "notes": "ledger crashlooping since 13:40 after chart bump 4.2.0"
}
```

**`utilization`** — capacity / rightsizing input.

```json
{
  "window": "15m",
  "node_count": 12,
  "cpu": {
    "allocatable_vcpu": 96,
    "requested_vcpu": 34,
    "used_vcpu": 21,
    "utilization_pct": 22
  },
  "memory": {
    "allocatable_gib": 384,
    "requested_gib": 150,
    "used_gib": 96,
    "utilization_pct": 25
  },
  "headroom": { "cpu_vcpu": 62, "memory_gib": 234 },
  "pressure": false,
  "top_consumers": [
    { "namespace": "search", "name": "indexer", "cpu_vcpu": 8, "mem_gib": 40 },
    { "namespace": "payments", "name": "ledger", "cpu_vcpu": 5, "mem_gib": 18 }
  ]
}
```

**`upgrade_readiness`** — readiness for a target K8s version.

```json
{
  "current_version": "1.30.5-gke.1000",
  "target_version": "1.32",
  "ready": false,
  "readiness_score": 62,
  "blockers": [
    {
      "severity": "high",
      "kind": "deprecated_api",
      "detail": "policy/v1beta1 PodSecurityPolicy in use (removed in 1.25+ path check)"
    },
    {
      "severity": "medium",
      "kind": "pdb_risk",
      "detail": "search/indexer PDB minAvailable=100% blocks node drain"
    }
  ],
  "deprecated_apis_in_use": [
    { "api": "batch/v1beta1 CronJob", "used_by": ["ops/reporting"] }
  ],
  "addon_compat": [
    { "addon": "ingress-nginx", "version": "1.9.0", "compatible": true }
  ],
  "recommended_window": "weekend maintenance; drain search last"
}
```

**`drift`** — live state vs GitOps baseline.

```json
{
  "baseline_ref": "git@github.com:org/fleet-config@a1b2c3d",
  "clean": false,
  "summary": { "added": 1, "removed": 0, "modified": 2 },
  "drift_items": [
    {
      "namespace": "payments",
      "kind": "Deployment",
      "name": "ledger",
      "field": "spec.replicas",
      "desired": 3,
      "live": 6,
      "drift_type": "modified"
    },
    {
      "namespace": "kube-system",
      "kind": "ConfigMap",
      "name": "manual-hotfix",
      "drift_type": "added"
    }
  ]
}
```

**`inventory`** — obtainability / topology snapshot.

```json
{
  "k8s_version": "1.30.5-gke.1000",
  "region": "us-central1",
  "node_pools": [
    {
      "name": "default",
      "machine_type": "e2-standard-4",
      "count": 3,
      "autoscaling": "3-8"
    }
  ],
  "namespaces": 24,
  "workloads": { "deployments": 61, "statefulsets": 7, "daemonsets": 9 },
  "features": {
    "workload_identity": true,
    "dataplane_v2": true,
    "managed_prometheus": false
  },
  "addons": ["gke-gateway", "cert-manager", "gke-managed-otel"]
}
```

### 2.7 Concurrency, atomicity, retention

- **Atomic writes** (same-directory temp + `fsync` + `os.replace`) — no torn reads, crash-safe (matches the `save_job_output` recipe).
- **Latest-wins** — each `(cluster, location, type)` is a single file, overwritten each cycle. No history in the handover layer (history lives in cron output / logs if needed).
- **Staleness** — readers honor `expires_at`; producers set a `ttl_seconds` sized to their scan cadence (e.g. a 5-minute health scan → `ttl ≈ 15m`).
- **Concurrent writers within a profile** — the persistent cron context and any transient worker share the profile; if both could write the same handover file, wrap the helper's write in an `flock` (the `_jobs_lock` pattern). Low risk (distinct types, low cadence) but cheap to harden.

### 2.8 No generic interface now; the write helper is the migration seam

We deliberately do **not** introduce a pluggable `FleetStore` backend interface while agents are co-located — it's unnecessary abstraction. Instead, _all writes funnel through `write_handover` and its one helper_, which gives the swappability where it matters without the ceremony:

- **Today (co-located):** the helper does an atomic local write to `/opt/data/fleet/…`.
- **Future (separate pods):** the _same_ helper instead **SCPs or HTTP-POSTs** the record into the **platform pod's** `/opt/data/fleet/clusters/<self>/…`. Only that one function changes.
- **The reader contract never changes.** The platform always just reads local files under `/opt/data/fleet/`. In the co-located world those files arrive by local write; in the split world they arrive by push. The platform (and its `SOUL.md`) cannot tell the difference, so nothing on the read side changes across the migration.

For the MVP we stay co-located; this section is the documented forward path.

---

## 3. Optional task delegation via the existing kanban board (secondary channel)

### 3.1 Delegation is optional — the platform decides

The platform agent can accomplish fleet operations **by itself**: it reads handover files, reasons, and emits GitOps PRs. Delegation to cluster subagents via the kanban board is an **optimization the platform chooses**, not a requirement. **With or without delegation, the functionality works.** This is the main difference between the initial three-agent implementation and the current design.

The platform delegates when it benefits from one of:

- **Context preservation.** Deep per-cluster investigation (RCA, upgrade analysis) would bloat the platform's context window. Delegating gives each cluster subagent a **fresh context**, and returns only a compact structured result to the platform.
- **Deterministic planning.** The kanban board provides structured orchestration the platform would otherwise hand-roll: parent/child **DAG dependencies**, **fan-in** aggregation, lifecycle states, **claim/lease** semantics (no double-execution) and retries. When a job has real structure or ordering, delegating makes the plan deterministic and inspectable rather than something the platform juggles in a single turn.

If neither applies (a quick, single-cluster action), the platform just does it directly and skips kanban.

### 3.2 Mechanism

- The kanban board is a **shared** store at the Hermes root (`<root>/kanban.db`) — the intended cross-profile coordination primitive. Co-located profiles share it directly.
- The platform (orchestrator) creates cards with `kanban_create(assignee=<cluster-profile>, body=<spec>, …)`.
- The Hermes **dispatcher** (running in the platform gateway) automatically spawns the assigned cluster subagent as a local worker (`hermes -p <cluster> chat -q "work kanban task <id>"`). No polling; the worker reads its pre-built context.
- The worker does the local work and returns a **structured result** via `kanban_complete(summary=…, metadata={…})`. `block_kind=needs_input` escalates to a human.
- **Fan-out:** the platform files the per-cluster cards in one burst (no `parents`, so they run in parallel), keeps its own card open while they run, reads each one's structured `metadata` via `kanban_show` as it settles, and synthesizes and acts there. Completing a card is the delivery of its `result`, so the image refuses a completion over unfinished fan-out children rather than let a dispatch receipt ship as the answer (#1010, `deploy/docker/patches/kanban_children_settled.py`). The `parents` edge still exists for what it says — a card gated until the listed cards finish — and an orchestrating _chat session_ (no worker card of its own) may still use a platform-assigned aggregation card as that child.

### 3.3 Read-only / declarative posture in delegated tasks

Delegated cards **do not imperatively mutate clusters**. They either (a) **validate** something locally and return findings, or (b) **generate declarative artifacts** (KCC/GitOps YAML). The platform opens the PR; Config Connector reconciles. This keeps cluster subagents read-only and puts all mutation in the GitOps path (with free rollback via PR revert).

### 3.4 Delegated tasks are visible in chat (thoughts emitted to the user)

Delegation is **transparent to the user**, not hidden plumbing:

- On `kanban_create`, the originating chat session is **auto-subscribed** to the task.
- The gateway's kanban notifier surfaces a card's **terminal** events (`completed`, `blocked`, `gave_up`, `crashed`, `timed_out`, `status`, `archived`, `unblocked`, `block_loop_detected`) back into that chat, plus `heartbeat` for mid-run progress: a worker calls `kanban_heartbeat(note=…)` at each milestone and the note joins a `⏳` line in the card's rolling progress message, delivered straight from the board and deliberately not waking the subscribed agent, so progress costs no LLM turn. Only the first note posts a message; the rest are edits to it, on any platform whose adapter supports editing, so a talkative card interrupts the space once. A terminal event settles that message (`✓` or `⏹`) and posts its own — the completion is the notification. `claimed` is not among them, and `kanban_comment` posts nothing to chat at all — a comment reaches a human only by causing a worker to act.
- The orchestrator (platform) narrates its plan when it decides to delegate ("delegating readiness checks to 3 clusters…") and reports the synthesized result as its own card's `result` once every delegated card has settled.

Net effect: delegated cluster subagents **emit their thoughts and results to the chat**, so the platform admin can watch the orchestration unfold and intervene (e.g. answer a `needs_input` block) without digging into internal state.

---

## 4. Worked CUJ — cross-cluster workload rebalancing (validation-then-declare)

**Trigger (handover channel):** the platform reads `utilization.json` for all clusters and detects **clusterA underutilized** and **clusterB overutilized / under pressure**. It decides some workloads should move from B to A.

**Decision (delegate or not):** the platform _chooses_ to orchestrate rather than do everything itself — to preserve its context and to make the plan deterministic. It uses the **validation-then-declare** pattern: cluster subagents **validate feasibility** (read-only); the platform **declares** the change (a single GitOps PR); KCC reconciles the actual move.

**Card graph (fan-out validation → decide on the platform's own card):**

```
Card A  (assignee = clusterA):  "Can you host workload W?"      ─┐  parallel — independent
Card B  (assignee = clusterB):  "Is it safe to evacuate W?"     ─┘  read-only checks
                                                                 ▲
platform's own card:  polls A and B (kanban_show), then "Decide & declare" here
```

The two validation cards are **parallel** (no ordering dependency — they're read-only checks). The make-before-break _execution_ ordering is handled later by KCC when it reconciles the PR, not by the agents.

`parents` is a **"runs after"** list, not a "belongs to" list: an edge points at what must finish _first_, and a card is unclaimable until every parent is settled. So A and B are created with **no** `parents` — in particular not the orchestrator's own in-flight card, which would deadlock them behind a card that is itself waiting on them. The image guards this: `deploy/docker/patches/kanban_scheduling.py` inverts an edge when a card blocks and is the last unfinished prerequisite of its own children, and records a `dependency_repaired` task event.

**Card A result — clusterA feasibility (`kanban_complete` metadata):**

```json
{
  "can_host": true,
  "workload": {
    "namespace": "search",
    "name": "indexer",
    "cpu_vcpu": 8,
    "mem_gib": 40
  },
  "headroom_after": { "cpu_vcpu": 54, "memory_gib": 194 },
  "constraints": [],
  "confidence": "high"
}
```

**Card B result — clusterB evacuation safety:**

```json
{
  "safe_to_evacuate": true,
  "workload": { "namespace": "search", "name": "indexer" },
  "blockers": [],
  "pdb_ok": true,
  "stateful": false,
  "in_flight_work": false,
  "notes": "no local PVs; safe to relocate"
}
```

**Platform decision & declaration (on its own card, once A and B settle):**

- If both green → generate the **relocation PR** (move the workload's manifest from clusterB's overlay to clusterA's overlay, or flip its target-cluster field) and open it. KCC/Config Sync performs the actual make-before-break move.
- If either red → do not declare; report blockers to the user, or `block_kind=needs_input` for a human decision.

```json
{
  "decision": "proceed",
  "workload": "search/indexer",
  "from": "clusterB",
  "to": "clusterA",
  "pr_url": "https://github.com/org/fleet-config/pull/482",
  "rationale": "clusterB CPU 88% / clusterA 22%; A has headroom; W stateless, no PDB block"
}
```

**Failure / compensation:** because the change is a **PR**, rollback is a revert. If a validation fails mid-flight, the decision step aborts the declaration (no partial move). Human-in-the-loop happens via `needs_input`.

**Why this is a good kanban CUJ:** genuine cross-cluster coordination (not just parallel independent tasks), a real orchestrator/executor split, an autonomous _decide-to-delegate_, both channels working together (FleetStore trigger → kanban orchestration), and a declarative, reversible outcome. It's the "global capacity orchestrator" concern graduating from a single do-it-all bot to platform-orchestrates / clusters-validate / KCC-executes.

---

## 5. Guardrails (summary)

1. **No agent-to-agent prompting.** Coordinate via handover files and the kanban board only.
2. **Constrain writes, free reads.** `write_handover` tool for status writes; plain file reads for consumption.
3. **Ownership by identity.** `cluster` and `location` come from the writer's profile, never a caller argument.
4. **Atomic writes.** Temp + rename on every handover write; readers honor `expires_at`.
5. **Write only via the tool/helper.** Never `write_file` handover paths directly.
6. **Read-only / declarative.** No imperative cluster mutation; emit KCC/GitOps PRs and let Config Connector reconcile.
7. **Delegation is optional and transparent.** The platform decides when to delegate; delegated work is visible in chat.
8. **Co-located MVP, one migration seam.** The write helper is the only place a future cross-pod transport changes; the reader contract is stable.

---

## 6. Open questions

### 6.1 Resolving "my cluster" at the front door

**Raised by** @dshnayder in review of [#439](https://github.com/gke-labs/kube-agents/pull/439#discussion_r3660282410): when a user says _"check the health of **my cluster**"_, it would be good if the very first agent — the Chat Agent front door — knew which cluster that means.

**Where this stands.** The memory half is now built. The Chat Agent is no longer stateless: it holds the `kube_agents_memory` provider, which tags each session's memories with the gateway identity and recalls that user's own facts into the prompt each turn, and `SOUL.md` §1.6 requires it to substitute every possessive with a concrete value before composing a `kanban_create` body. So _"my cluster"_ resolves at the front door for any user who has stated their cluster once. The remaining gap is the **cold start** — a user who has not said it yet, where the front door must fall back to the roster.

**Why the roster fallback is hard.** It carries almost no per-cluster signal:

- **Names are lossy.** `profile_name()` (`agents/platform/scripts/cluster_agent_profile.py`) builds `cluster-{project}-{cluster}-{location}`, sanitizes it, and truncates past 63 characters with an 8-char sha1 suffix. Component boundaries are also ambiguous when a project or cluster name itself contains hyphens.
- **Descriptions are identical.** Every Cluster Agent is scaffolded from the same `agents/cluster/CAPABILITIES.md`. The shared-role grouping in `list_agents` exists precisely because that string is the same for all of them.

**Constraints on any fix.**

1. Do not re-enable the `file` toolset on the `default` profile, and do not grant the front door any terminal, exec, or infrastructure surface — the lockdown is the point. `memory` is the one deliberate exception, and it is narrow: the toolset name is listed only to pass the provider-injection gate, and the operator re-adds it to `agent.disabled_toolsets` if the built-in store is ever switched on, so the front door never gets a second, unscoped memory tool.
2. Do not resolve it by asking the `platform` agent. The ask is specifically that the _first_ agent knows; a round trip defeats it.
3. Do not fold cluster identity into the grouped role description. `list_agents` groups on that string; making it per-cluster-unique would restore the N× repetition the grouping removes. Identity belongs on the per-agent line _inside_ a group.
4. Degrade, never raise. `list_agents` is the front door's only routing tool.

**Sketched direction** for the cold-start case, in increasing order of cost — each step is useful alone:

| Step | Change                                                                                                                         | Notes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| ---- | ------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1    | Front-door rule: if exactly one `cluster-*` agent exists, "my cluster" means that one; otherwise ask once, listing candidates. | Prompt-only (`agents/chat/SOUL.md`), no new state. Removes the silent-wrong-cluster failure but does not resolve the reference.                                                                                                                                                                                                                                                                                                                                                                                |
| 2    | Surface structured `{project, cluster, location}` per agent in `list_agents`.                                                  | Reuses `read_cluster_identity()` (`agents/platform/scripts/cluster_agent_profile.py`), already documented as the robust inverse of the sanitized/hashed name. The router can import it: the Dockerfile colocates `agents/chat/scripts/` and `agents/platform/scripts/` under `$HERMES_HOME/scripts`. Note that helper currently catches only `FileNotFoundError`/`yaml.YAMLError`, so it needs an `OSError` guard to satisfy constraint 4.                                                                     |
| 3    | ~~A narrow per-user default-cluster preference.~~ **Done** — via general per-user memory rather than a scoped router tool.     | `kube_agents_memory` on the `default` profile, scoped by a tag derived from the gateway `user_id`. One sub-question resolved the hard way: in a **shared thread** `user_id` is whoever opened the session, because `build_session_key()` omits the participant id there unless `thread_sessions_per_user` is set — so a thread's participants would share one identity. The provider detects that case and disables personal memory rather than crossing users, which makes per-user memory DM-only by design. |

Step 2 remains the open piece: it is what makes the cold start — and the shared-thread case, where personal memory is deliberately unavailable — resolvable without a round trip.
