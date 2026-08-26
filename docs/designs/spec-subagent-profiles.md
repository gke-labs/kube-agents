# Declarative subagent profiles

- **Author:** [@bnaylor]
- **Date:** 2026-08-24
- **Status:** draft, for review
- **Companions:** the A2A payload spec (`spec-a2a-payloads.md`) - envelope, subjects, task
  lifecycle; the NATS deployment spec (`spec-nats-deployment.md`) - streams, accounts,
  connection-time authz

## Purpose

This spec defines how a subagent becomes a declarative, first-class thing: a profile the
operator knows about, a pod per task, and a stream of status back to whoever asked. It
covers the profile format (a CRD, argued below), how a profile becomes a pod, how thinking
and status stream back to the originating session over the bus, and the task lifecycle -
spawn, done, killed, orphaned.

It ends with two worked profiles (the platform agent and a per-cluster agent) and an
explicit accounting of what the kanban board does today that this framework does not.
That last section is the checklist for retiring kanban. Nothing in it is allowed to
disappear silently.

## What a subagent is today

There is one operator-deployed pod per `PlatformAgent` CR. Everything we call an agent -
the chat front door, the platform agent, every per-cluster cluster agent - is a Hermes
profile: a directory under `$HERMES_HOME/profiles/<name>` on the shared data PVC, holding
`SOUL.md`, `config.yaml`, and skills (`agents/platform/scripts/profile_scaffold.py:1-14`).
Cluster agent profiles are scaffolded at runtime, one per managed cluster
(`agents/platform/scripts/cluster_agent_profile.py:66-78`), with names sanitized and
truncated to fit - which is why recovering a cluster's identity from its agent name is
already documented as unreliable (`docs/designs/agent-communication.md`).

A subagent invocation is a fresh OS subprocess in the same container: an orchestrator
files a kanban card, and the dispatcher spawns
`hermes -p <profile> chat -q "work kanban task <id>"` against the shared board
(`deploy/docker/patches/kanban_result_required.py:105`). Isolation between subagents is
whatever a process boundary gives you. They share the PVC, the pod's service account,
and the network identity. Concurrency is capped board-wide at 2
(`k8s-operator/api/v1alpha1/common_types.go:218-249`) because each worker is a few
hundred MiB inside one pod's memory budget.

The A2A demo already ran the target shape: a delegator builds a `V1Pod` in application
code (the demo's chatops delegator; the demo repo is not part of this repository), the
worker learns its task id from
env and pulls the task itself off JetStream, output streams back over the bus, and the pod
is deleted when done. It worked, and it also accumulated exactly the hacks this spec is
here to remove: pod specs assembled in application code, a `[thinking]` string prefix
standing in for a message type, an in-memory delegation map that dies with the delegator,
and no cancel path at all.

## The profile format is a CRD

The profile is a namespaced custom resource, `AgentProfile`, in the operator's existing
`kubeagents.x-k8s.io/v1alpha1` group. The argument:

- **Profiles are created at runtime, so install-time config is out.** Cluster agent
  profiles come and go with the clusters they watch - the reconciler creates one per
  discovered cluster and prunes on deletion (`agents/platform/scripts/cluster_agent_reconcile.py`).
  Helm values and image-baked templates cannot express that. Whatever the format is, it
  needs a create/delete API with admission validation, and the API server already is one.
- **The profile is where identity gets anchored.** The point of subagents-as-pods is that
  a subagent becomes something the enforcers can see. Three things have to agree for
  that: the KSA the pod runs as, the NATS user the auth callout grants (the deployment
  spec's identity-to-permissions map), and the persona the pod boots. One object the
  operator renders all three from, or they drift apart.
- **Defining an agent is a privileged act.** A profile grants bus permissions and a
  workload identity. With a CRD, who may create or mutate profiles is ordinary RBAC on
  one resource type, auditable in the API server log like everything else.
- **It's the house pattern.** The operator already reconciles `PlatformAgent` and
  `AgentPlugin`. Profile content mounted from an OCI image volume already exists for
  plugins (`k8s-operator/internal/controller/platformagent_manifests.go:1954`). A third
  CRD is more of the same machinery, not new machinery.

Rejected alternatives:

- **Today's shape** (image-baked templates plus PVC directories). Not declarative,
  invisible to Kubernetes, and the roster is a directory listing. This is what we're
  replacing.
- **ConfigMaps.** No schema, no status, and ConfigMap write access in the namespace
  would quietly become the right to mint agents with topic write grants.
- **Agent cards on the bus as the definition.** Cards are presence, not desired state -
  and the bus deciding who may exist on the bus is circular. Cards stay what the payload
  spec says they are: a runtime directory, published on startup.
- **A per-persona CRD zoo** (`ClusterAgent`, `AuditAgent`, ...). The personas differ in
  data, not shape. One resource type.

One deliberate non-decision: there is no separate template/instance split, no
`AgentProfileTemplate`. A profile CR is concrete - one per agent identity. The
per-cluster case is the reconciler stamping out one CR per cluster, exactly as it stamps
out profile directories today. The scaffolder is the template engine. We don't need a
second one.

Relationship to the architecture set: `docs/architecture/06-api-and-data-contracts.md`
defines `kind: Agent` and 08 reconciles it into one isolated pod per agent.
`AgentProfile` is the concrete stage-3 resource on that road - profile-shaped identity,
pod-per-task execution. Whether the two objects merge or `AgentProfile` supersedes
`Agent` is settled in the 01-08 revision, which this design feeds; nothing here forecloses
either answer.

### What the CRD deliberately does not hold: task state

There is no `AgentTask` CR and no task status mirrored into the API server. Tasks live
on the bus: submission on `a2a.tasks.{profile}.{taskId}.in`, every event on
`a2a.tasks.{profile}.{taskId}.events`, status answered by replay. Mirroring that into etcd would create a second source of truth
that is guaranteed to lag the first, plus an API-server write per status event. The
kanban board is a task database bolted to the side of the harness. The durable stream is
the task database now, and it comes with the audit story attached. The only Kubernetes
object a task gets is the Job that runs it, and that object carries no task semantics
beyond "the process ran here."

## The AgentProfile resource

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: AgentProfile
metadata:
  name: platform
spec:
  description: >-
    One-paragraph routing description.  Feeds the agent card and the roster.
  persona:
    image: <registry>/kube-agents/persona-platform:2026.08 # SOUL.md, AGENTS.md, skills/
  harness:
    image: <registry>/kube-agents/agent-worker:2026.08 # headless-contract runner + adapter
    model: model-default # via LiteLLM, as today
    maxTurns: 250
  bus:
    publishTopics:
      - agent.platform.upgrade-readiness
      - agent.platform.version-skew
    subscribeTopics:
      - shared.blueprint
  identity:
    serviceAccountName: agent-platform
  lifecycle:
    activeDeadlineSeconds: 3600
    ttlSecondsAfterFinished: 600
  concurrency: 2
  resources:
    requests: { cpu: 250m, memory: 512Mi }
    limits: { cpu: "1", memory: 2Gi }
```

| Field                                   | What it is                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| --------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `description`                           | The routing blurb - today's `CAPABILITIES.md` first line. Rendered into the A2A agent card.                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `persona.image`                         | OCI artifact holding the persona: `SOUL.md`, `AGENTS.md`, skills, persona config. Mounted read-only via image volume, the `AgentPlugin` mechanism - and it inherits that mechanism's availability gate: the operator already skips image-volume mounts below Kubernetes 1.35 and logs it (`platformagent_manifests.go`), so no new fallback is invented here. (Decided 8/24: OCI-only, no `configMapRef` variant.)                                                                                                                                        |
| `harness.image`                         | The worker image: a harness speaking the headless CLI contract, wrapped by the bus adapter (below). The harness is a container image choice, nothing more.                                                                                                                                                                                                                                                                                                                                                                                                |
| `harness.model`, `maxTurns`             | Model routing and the turn budget, as in profile config today.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| `bus.publishTopics` / `subscribeTopics` | Topic grants beyond the task subjects every executor gets. The operator renders these into the auth callout's identity-to-permissions map; the deny-by-default posture is the deployment spec's.                                                                                                                                                                                                                                                                                                                                                          |
| `identity.serviceAccountName`           | The KSA the pod runs as. Optional. Absent means the operator creates a KSA with **zero** RBAC bindings, which is the default posture - the token exists to authenticate to NATS, not to talk to the API server.                                                                                                                                                                                                                                                                                                                                           |
| `clusterRef`                            | (Cluster agents only.) `{projectId, cluster, location}` as structured fields. The operator renders the scoped read-only kubeconfig from it. This also ends the parse-the-sanitized-name problem: the cluster identity is spec data, not a naming convention.                                                                                                                                                                                                                                                                                              |
| `lifecycle.activeDeadlineSeconds`       | Hard ceiling on one task's wall clock, enforced by Kubernetes. The clock starts at Job spawn - queue time is bounded separately by `queueTimeoutSeconds`.                                                                                                                                                                                                                                                                                                                                                                                                 |
| `queueTimeoutSeconds`                   | Optional, default 3600. How stale a queued submission may be before the dispatcher refuses to run it: at dequeue, if the message's JetStream server ingest timestamp is older than this, the dispatcher acks and publishes terminal `failed` (`reason: queue-deadline-exceeded`) instead of spawning a Job. A burst past `concurrency` cannot silently execute hours-stale diagnostics. The arbiter is the server ingest time, deliberately - the envelope's `ts` is publisher-asserted, and a freshness guard keyed to a spoofable field is not a guard. |
| `lifecycle.ttlSecondsAfterFinished`     | How long a finished Job lingers for `kubectl` inspection before GC.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `concurrency`                           | Max simultaneous pods for this profile. Replaces the board-wide `max_in_progress`, per profile instead of global.                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| `resources`                             | Pod resources. Default is the demo-proven class: 250m/512Mi requests, 1 CPU/2Gi limits.                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |

Everything here is dark content: the dispatcher and the profile CRD render nothing unless
the mode switch says `next`.

## How a profile becomes a pod

**Submission is a message, not an API call.** An orchestrator delegates by publishing a
`kind: message` envelope to `a2a.tasks.{profile}.{taskId}.in` - the profile is the
addressee token, and `to` agrees with it. Profile names are reserved addressees in the
directory, and "who may delegate to which profiles" is a connect-time publish grant on
exactly these subjects. That is the entire client-side surface -
the requester needs zero Kubernetes permissions to delegate. (The demo's delegator
carried a Role with pod create/delete/watch; that goes away.)

**The dispatcher turns messages into Jobs.** A dispatcher controller in the operator
binary holds one durable consumer per profile on `a2a.tasks.{profile}.*.in`. The
dispatcher and the janitor run under the operator manager's leader election - the
`--leader-elect` machinery the operator already ships, required on under `mode: next` -
so "one dispatcher" and "exactly one final event" are elected properties, not deployment
accidents. Scaling the operator for HA changes nothing here: followers hold no
consumers. Only a
task-starting submission renders a Job: before creating, the dispatcher checks the
task's `…events` subject, and empty means new task. A task with events already is not
the dispatcher's business - follow-ups, steers, and cancels for a live task belong to
the in-pod adapter, and `…in` traffic for a task with a terminal event is acked with a
warning and no Job. Without that check, a follow-up or cancel arriving after
`ttlSecondsAfterFinished` GC'd the Job would resurrect a completed task, and every steer
against a running Job would burn an API-server create on a 409. Session-addressed
subjects are not in its grants at all. Per-profile consumers also isolate backlogs: a
capped or crash-looping profile's unacked redeliveries cannot head-of-line block another
profile's dispatch. Notes on the mechanics:

- The Job name derives from the taskId, so Job creation is idempotent - a JetStream
  redelivery hits `AlreadyExists` and acks. Combined with envelope dedup this is the
  claim/lease story: there is one dispatcher, and creates are idempotent, so
  double-execution needs no lock table. The events-check above also closes the GC
  window: a redelivery arriving after `ttlSecondsAfterFinished` reaped the finished Job
  finds the terminal event on the stream and does not re-create.
- The dispatcher acks _after_ the Job is created. A dispatcher restart replays
  unacked submissions from the durable consumer - the queue is the stream, and there is
  no in-memory delegation map to lose.
- `concurrency` is enforced here: submissions beyond a profile's cap are redelivered
  with backoff until a slot frees. Nothing is dropped; the stream holds the backlog.
- A new profile's first dispatch waits on its `BusCredentialsReady` status condition -
  the deployment spec owns why (auth-callout propagation). Submissions queue on the
  stream meanwhile.
- At dequeue the dispatcher checks queue staleness against `queueTimeoutSeconds` using
  the message's server ingest timestamp (see the field table for why not `ts`), and
  refuses stale work with a terminal `failed` rather than running it late.
- The demo created the pod first and published the task second, and papered over the race
  with a stream lookup. This ordering is the fix: the pod exists _because_ the message
  is already durable, so the worker's task fetch cannot miss.

**The Job.** `backoffLimit: 0` - Kubernetes does not get to re-run an LLM task that may
have half-performed side effects. Retry is a decision for the requester, made against the
terminal event (see lifecycle). `restartPolicy: Never`, `activeDeadlineSeconds` and
`ttlSecondsAfterFinished` from the profile. The pod spec follows the demo's worker
posture: non-root, scratch on an emptyDir, no secrets. Two deltas from the demo:

- **Model auth is Workload Identity, not an API key.** The harness talks to the model
  through the Vertex backend (or LiteLLM as today). No per-worker Anthropic key in a
  Secret.
- **The pod does mount a KSA token - for the bus.** The demo set
  `automountServiceAccountToken: false` and connected to an unauthenticated NATS. Here
  the deployment spec's auth callout validates a KSA token at connect, so the pod gets a
  projected token volume, audience-bound to NATS, short-lived. API server access is
  still nil for the default profile (a KSA with no RoleBindings has a name and nothing
  else). Automount stays off; the projected volume is explicit.

Env is minimal: `TASK_ID`, `PROFILE`, `NATS_URL`. Everything else - prompt, correlation,
context - is in the task message the adapter fetches from the stream by subject.

**The adapter.** Inside the pod, a thin adapter sits between the bus and the harness.
It fetches the task message, opens its own ephemeral consumer on the task's `…in`
subject positioned just after that message - this is where live input arrives, because
the `…in` subject has two reader roles by design: the dispatcher for new tasks, the
executor for everything after the submission - publishes `submitted`, drives the harness
over the headless contract (`-p`, stream-json in/out), maps the harness's output stream
onto A2A events (next section), forwards cancels, steers, and follow-up input onto the
harness stdin, publishes the terminal event, and exits with a matching code. Ephemeral,
deliberately: a rehydrated session's fresh pod opens a fresh consumer with no durable
name to collide with its predecessor's. The adapter is stage 3 code. The
client library under it (envelopes, dedup, resilience contract) is stage 1's.

**Discovery.** The operator publishes an agent card to `a2a.agents.{profile}` when a
profile lands and a tombstone when it is deleted, rendered from `description`. Deletion
is finalizer-ordered: the tombstone publish precedes finalizer removal, and reconcile
republishes the card whenever a live CR's directory entry is missing or tombstoned -
level-triggered, so a crash on either side of a delete converges instead of leaving a
stale card routing traffic to a profile that no longer exists. The chat
front door's roster becomes a read of the directory stream instead of a listing of
profile directories. Same information, but it exists whether or not any worker is
running.

Spawn latency is the demo's: roughly 5-10 seconds to first streamed output with a warm
node and pre-pulled image. Fine for delegated tasks, which today sit in a 5-second
dispatch poll anyway.

## Thinking and status, back over the bus

Everything a worker emits rides `a2a.tasks.{profile}.{taskId}.events` as the payload
spec's two
event kinds. The originating session is already subscribed - streaming is how the bus
works. Correlation is `taskId` for the task and `correlationId` for the thread. A child
task inherits its parent's `correlationId`, so one identifier still spans the user's
question, the delegation, and the result.

The demo taught the anti-lesson here: it shipped thinking as a `[thinking] ` string
prefix inside message chunks, progress as a `[progress] ` prefix, and made the UI
pattern-match text. The rendering pain was immediate and is documented in its tweak
notes. Prefixes are not a protocol. Instead:

**`status-update` carries state transitions only** - `submitted`, `working`,
`input-required`, and one terminal event with `final: true`. Not progress, not tool
chatter.

**`artifact-update` carries the streams, as named artifacts.** Four reserved names:

| Artifact name | Content                                                    | Producer                                       | Default consumer                                                                 |
| ------------- | ---------------------------------------------------------- | ---------------------------------------------- | -------------------------------------------------------------------------------- |
| `result`      | The deliverable, chunked per A2A chunking rules            | harness output                                 | posted to the requester verbatim, as `kanban_complete`'s `result` field is today |
| `thinking`    | Thinking/reasoning deltas                                  | adapter, from the harness stream               | debug views only                                                                 |
| `activity`    | Tool-call trace: one entry per tool invocation             | adapter                                        | debug views; always in the audit replay                                          |
| `progress`    | Agent-authored milestones - the heartbeat-note replacement | an explicit progress tool exposed to the agent | rendered to chat at zero model cost                                              |

This maps one-to-one onto what exists. Kanban heartbeat notes become `progress` updates:
the gateway's notifier can render them into a rolling chat line without waking any model,
which preserves the property that mid-run visibility costs no LLM turn
(`deploy/docker/patches/kanban_progress_lines.py` is the bar to meet). The chat display
modes survive as a rendering policy: default mode shows `progress` and terminal events,
debug mode adds `activity` and `thinking` - the same split the Google Chat `mode` field
draws today (`platformagent_manifests.go:1348-1371`).

Artifact names are data, so the set can grow without touching the envelope or the payload
spec. These four are reserved so that renderers and the audit tooling can rely on them.

**Input flows the other way on the same subjects.** A worker that needs a human lands on
`input-required` with a message saying what it needs - the `needs_input` escalation, now a
first-class task state. The requester (or the gateway, relaying a human) publishes a
follow-up `message` on `…in`, and the adapter injects it into the harness stdin. The
same path handles unsolicited mid-run steering, which today is `kanban_comment` plus a
patched injection hook. The stdin stream is the inject API this harness family actually
has. Nothing to black-hole.

## Lifecycle

**Spawn.** Covered above: durable submission, idempotent Job create, adapter publishes
`submitted` then `working`.

**Done.** The adapter publishes the terminal `status-update` (`completed`, `failed`,
`rejected`) with `final: true`, closes its bus connection, exits. Job completes, TTL
reaps it. Nobody deletes pods by hand and no component needs delete rights for the happy
path. The stream retains the full event history for the audit window regardless of pod
GC - replay is not tied to the pod's existence.

**Killed.** Two layers, on purpose:

- _Protocol cancel:_ `kind: cancel` on `…in`. The adapter interrupts the harness and
  publishes terminal `canceled` (or the task wins the race and completes - both legal per
  the payload spec). The demo defined this and never built it. Here it is adapter table
  stakes and a conformance case.
- _Enforcer kill:_ delete the Job. This is the pod-shape payoff - killing a subagent is
  a Kubernetes verb with Kubernetes RBAC, not an API the harness may or may not
  implement. The janitor (below) writes the terminal event on the dead task's behalf.
  `activeDeadlineSeconds` is the same path triggered by the clock.

**Evicted.** Node drains, spot preemptions, and GKE upgrades deliver `SIGTERM` with a
grace period, and the adapter MUST trap it: flush the pending output buffer, publish
terminal `failed` with `reason: worker-evicted`, exit 143. That keeps an infrastructure
eviction distinguishable from an agent crash in the audit trail and in the breaker's
failure classes - the same infra-vs-agent distinction the kanban board's forgiveness
classes draw today.

**Orphaned.** A worker can die without a terminal event - OOM, node loss, image bug.
The dispatcher doubles as the janitor: it watches the Jobs it created, and when a Job
reaches a terminal phase (or vanishes) while the task's event stream has no `final`
event, it publishes terminal `failed` with a reason naming the evidence
(`pod-failed-without-final-event`, `deadline-exceeded`, `job-deleted`). The demo's sweep
did exactly this and it worked. The janitor inherits the pattern with the Job as the
source of truth instead of a poll over pods.

The synthesize is a compare-and-swap, not a read-then-write: the janitor publishes its
terminal event with the expected last subject sequence it observed when it found no
terminal - so a dying pod's SIGTERM flush racing the sweep wins cleanly, the janitor's
publish is rejected, and it re-reads instead of double-finalizing. "Exactly one final
event" is arbitrated by the write, not by the check before it; whichever writer loses
lands in the warn-and-drop path like any other post-final event.

This is the dispatcher's half of the payload spec's orphaned-task answer; the gateway
sweeps its own chat sessions the same way (the ratified 8/24 split - every task's
supervisor is its janitor). The janitor's grant is publish on its own profiles'
`…events` subjects - subject-level, since NATS permissions cannot see the envelope
`kind`; that a janitor emits only terminal `status-update` is a conformance assertion,
not a connect-time control. The grant is in the identity-to-permissions map like every
other, and synthesized events carry the janitor's own identity in `from`, so replay
always distinguishes "the worker said failed" from "the janitor declared it dead."

**What is deliberately absent: automatic retry.** Today the board charges a retry
budget, forgives infrastructure deaths, and trips a breaker on repeat offenders. This
framework marks the task failed with an honest reason and stops. Whether to resubmit is
the requester's call, made with the failure in hand. The gap this opens is real and is
in the kanban table below.

**Restart safety.** Dispatcher state is the durable consumer position plus the live Jobs,
both recoverable. Worker state is the event stream. The only in-memory thing anywhere
is the harness process itself, and its death is the orphan path.

## Worked profile: the platform agent

The platform agent is the privileged doer - provisioning, fleet audits, the GitOps write
path - and today it is also a kanban worker like any other when the front door delegates
to it. As a profile:

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: AgentProfile
metadata:
  name: platform
spec:
  description: >-
    Fleet-wide GKE architecture and the privileged doer.  Provisioning and cluster
    lifecycle, multi-tenancy and RBAC isolation, fleet-wide audits, Config Connector and
    GitOps operations, and any change proposed as a Pull Request.  Owns cluster agent
    lifecycle and acting on cluster agents' proposed fixes.
  persona:
    image: <registry>/kube-agents/persona-platform:2026.08
    # SOUL.md, AGENTS.md, governance SOPs, the platform skills, persona config
    # (toolsets: mcp-platform_control, mcp-gke, mcp-developer_knowledge, memory read-only)
  harness:
    image: <registry>/kube-agents/agent-worker:2026.08
    model: model-default
    maxTurns: 250 # today's value, set for fleet-audit remediation loops
  bus:
    publishTopics:
      - agent.platform.upgrade-readiness
      - agent.platform.version-skew
    subscribeTopics:
      - shared.blueprint
  identity:
    serviceAccountName: agent-platform
    # The exception to the zero-RBAC default: this KSA carries the Workload Identity
    # binding for gcloud/Config Connector reads and the GitOps write path, i.e. the
    # grants the shared pod SA holds today - now scoped to this persona alone.
  lifecycle:
    activeDeadlineSeconds: 7200
    ttlSecondsAfterFinished: 600
  concurrency: 2
  resources:
    requests: { cpu: 250m, memory: 512Mi }
    limits: { cpu: "1", memory: 2Gi }
```

The interesting delta from today is the identity line. Right now the platform persona's
authority is the pod's authority, shared with every other profile in the container. As a
profile it is the only persona whose KSA carries real grants, and a cluster agent
compromise no longer sits one process boundary away from the GitOps credentials.

The governance cron personas (compliance audit, cost analysis, and friends) are not
separate profiles in this pass - they run on the platform persona today and keep doing so.
When cron moves to Kubernetes Jobs in stage 4, each job becomes a submission addressed to
`platform` (or graduates to its own profile if its authority should be narrower). The
CRD needs nothing new for that.

## Worked profile: a cluster agent

One CR per managed cluster, created and pruned by the reconciler exactly as profile
directories are today - except the reconciler now writes an API object instead of a
directory, so the roster is visible to `kubectl` and the cluster identity is structured
data instead of a sanitized name.

```yaml
apiVersion: kubeagents.x-k8s.io/v1alpha1
kind: AgentProfile
metadata:
  name: cluster-acme-prod-prod-a-us-east1
  labels:
    kubeagents.x-k8s.io/persona: cluster
spec:
  description: >-
    Read-only SRE scoped to exactly one GKE cluster.  Live runtime state and workload
    debugging: crash loops, OOMKills, unschedulable pods, autoscaling behavior, storage
    binding.  Returns a grounded RCA and a proposed manifest patch; never mutates, never
    opens PRs.
  clusterRef:
    projectId: acme-prod
    cluster: prod-a
    location: us-east1
  persona:
    image: <registry>/kube-agents/persona-cluster:2026.08
    # SOUL.md, AGENTS.md, the gke-* debugging skills, persona config
    # (read-only gke MCP + developer_knowledge; platform_control deliberately absent)
  harness:
    image: <registry>/kube-agents/agent-worker:2026.08
    model: model-default
    maxTurns: 50
  bus: {} # task subjects only; no topic grants
  identity: {} # operator-created KSA, zero RoleBindings
  lifecycle:
    activeDeadlineSeconds: 1800
    ttlSecondsAfterFinished: 600
  concurrency: 2
  resources:
    requests: { cpu: 250m, memory: 512Mi }
    limits: { cpu: "1", memory: 2Gi }
```

The operator renders the scoped read-only kubeconfig from `clusterRef`, replacing the
scaffold-time kubeconfig pinning in `cluster_agent_profile.py`. The read-only posture is
unchanged. What changes is that "read-only" stops being a property of a config file on a
shared volume and becomes a property of a principal with no write grants anywhere.

Fan-out looks the same as today's rebalancing CUJ, minus the board: the platform agent
publishes one submission per cluster profile, each cluster worker streams `progress` and
lands a `result` artifact (the structured feasibility verdict that used to ride
`kanban_complete` metadata), and the platform agent submits its own aggregation task -
or just synthesizes in its current turn - with the results in hand.

## What kanban does today that a profile does not cover

Kanban is a patched SQLite board doing real work: dispatch, locking, retry management,
chat notification, DAG scheduling. Stage 3 retires it, so here is the full inventory and
where each piece lands. "Gateway" means the chatops gateway design owns rebuilding it -
this spec only guarantees the bus carries what that rebuild needs.

| Kanban capability (where it lives today)                                                                                                                                                                                                       | Under profiles                                                                                             | Verdict                          |
| ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- | -------------------------------- |
| Card body as the worker's pre-built context                                                                                                                                                                                                    | The submission message's parts                                                                             | Covered                          |
| Dispatch: 5s poll plus wake-file nudge (`kanban_wake_nudge.py`)                                                                                                                                                                                | Push durable consumer                                                                                      | Covered                          |
| Claim/lease, `host:pid` fencing, 15-min lease (`kanban_ownership.py`, `kanban_scheduling.py:540-680`)                                                                                                                                          | Single dispatcher, idempotent Job create keyed on taskId, envelope dedup                                   | Covered                          |
| `max_in_progress` board cap (`common_types.go:218-249`)                                                                                                                                                                                        | `spec.concurrency`, per profile                                                                            | Covered                          |
| Structured result posted verbatim, summary clipping (`kanban_result_required.py`)                                                                                                                                                              | `result` artifact + terminal status; rendering moves                                                       | Gateway                          |
| Heartbeat notes as a rolling, edited chat line at zero LLM cost (`kanban_progress_lines.py`)                                                                                                                                                   | `progress` artifact carries the notes; the rolling-message rendering must be rebuilt                       | Gateway                          |
| Auto-subscribe of the originating thread, inheritance to child cards, at-least-once delivery cursor, wake policy - completed does not wake the creator (`kanban_notify_propagate.py`, `kanban_auto_subscribe.py`, `kanban_notify_delivery.py`) | `correlationId` + durable replay are the substrate; subscription and wake policy are gateway session state | Gateway                          |
| Report-by-thread `incidents` store, so "apply Option A" replies have context (`kanban_notifier.py`)                                                                                                                                            | No bus home; gateway session state                                                                         | Gateway - flagged to that design |
| `needs_input` escalation to a human                                                                                                                                                                                                            | `input-required` state, first-class                                                                        | Covered                          |
| Mid-run steering: `kanban_comment` injected into a running worker (`kanban_comment_status.py`)                                                                                                                                                 | Follow-up message on `…in`, adapter injects via harness stdin                                              | Covered (adapter work)           |
| Exit-without-terminal reaper stamping `protocol_violation` (`kanban_guardrail_exit.py`)                                                                                                                                                        | Janitor synthesizes terminal `failed` with reason                                                          | Covered                          |
| Attachments (`kanban_attach`)                                                                                                                                                                                                                  | A2A FileParts; backing store is the payload spec's open question                                           | Covered, pending that call       |
| Board inspection (`kanban_list`, board-health cron)                                                                                                                                                                                            | `kubectl get jobs`, stream replay, the `runtime-state` KV                                                  | Covered, different tools         |
| **Parent/child DAG, ready-gating, fan-in cards fed every parent's metadata, self-parenting repair** (`kanban_scheduling.py`)                                                                                                                   | Nothing. The orchestrating agent sequences its own fan-out and feeds results forward itself                | **Gap**                          |
| **Retry budget, forgiveness classes (pod-replaced vs died-in-place), failure-limit breaker, crash-loop fingerprinting** (`kanban_scheduling.py` part 3, `kanban_board_health.py`)                                                              | Nothing. The janitor states the failure honestly; nobody counts them                                       | **Gap**                          |

The two gaps, plainly:

**DAG scheduling.** The board is a small workflow engine: children unclaimable until
parents settle, fan-in cards that aggregate parent metadata, and plan state that survives
an orchestrator crash. This framework replaces it with "the orchestrator sequences."
The design of record (`docs/designs/agent-communication.md`) already treats delegation as
an optimization the platform _chooses_,
not a requirement, which is most of why I think we can live without an engine at stage 3 -
the fan-out patterns in use are one level deep. What is genuinely lost is crash-safe
plan state: an orchestrator that dies mid-fan-out must reconstruct its plan from task
replay rather than from a board. If that proves painful in practice, the cheap fix is
the orchestrator journaling its plan to a topic, not a workflow engine on the bus.

**Failure management.** The board's retry budget distinguishes "the pod got replaced,
forgiven" from "the agent crashed, charged," and trips a breaker on repeat offenders. We
keep the _signal_ (the janitor's reason field carries the same infra-vs-agent distinction,
straight from the Job status) but lose the _policy_. A crash-looping profile today gets
blocked by the breaker. Under this spec it fails one submission at a time, forever, at
one pod per failure. `concurrency` bounds the blast radius but does not stop the loop.
A minimal dispatcher-side breaker - N consecutive janitor-declared failures for a profile
pauses dispatch to it and raises an event - is a stage 3 requirement (decided 8/24).

## Payload spec reconciliation

Three small items to fold back into the payload spec rather than fork here (folded 8/24):

- An additive `from.profile` field (display/routing, same trust rules as the rest of
  `from`), so renderers stop parsing session names. Additive within the major, per its
  own field rules.
- The four reserved artifact names, so conformance can assert renderers and audit tooling
  agree on them.
- The janitor as the answer to its orphaned-task open question, with the permission
  scoping above.

## Open Questions ([@bnaylor])

- ~~**Janitor authority sign-off.**~~ Ratified 8/24; the payload spec records it, and
  the deployment spec's map amendment carries the janitor's static entry.
- ~~**Who creates cluster AgentProfiles.**~~ Decided 8/24: the operator. Discovery
  moves into a controller, in Go, and agents never write identity objects. No
  flexibility is lost, and it is worth restating why: spawning a worker from an existing
  profile is a bus message, not a CR write - the interactive path never touches the API
  server. The only runtime CR writer is the operator's instance stamper.
- ~~**The failure breaker.**~~ Decided 8/24: stage 3 scope, as specified in the failure
  management section.
- ~~**Persona packaging.**~~ Decided 8/24: OCI-only. One versioned path; dev friction is
  a tooling problem (a push target is seconds), not a second mechanism. Revisit only if
  the iteration friction proves real.
- ~~**Per-task bus credentials.**~~ Decided 8/24: lands with the authority work - it is
  the same attenuation machinery, and that stream owns it. Stays reserved as a named
  tightening; stage 3 ships profile-level credentials.
- ~~**Worker session naming.**~~ Decided 8/24: the animals stay - `<profile>-<animal>`
  per run, with `from.profile` carrying the structure.
