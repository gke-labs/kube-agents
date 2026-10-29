# Running the A2A gateway at more than one replica

## Purpose

The A2A gateway runs as one replica with a `Recreate` strategy, and that count is an invariant,
not a capacity setting (`buildA2AGatewayDeployment`). Every rollout, node drain or crash is a gap
with no gateway: chat turns aren't taken, task events aren't relayed, and the HTTP doors refuse.
This document decides how the gateway runs at more than one replica, in the order the build lands.

The goal is availability: a gateway that keeps answering through a rollout and survives losing a
node. Throughput isn't the constraint today. One gateway is a small, I/O-bound process, and the
work it fronts runs in the session pods, the bridge and the profile workers.

It builds on the relay's delivery model (`spec-chatops-gateway.md`, the relay sections) and on the
bus going highly available (the NATS HA work). Gateway HA without bus HA moves the single point of
failure rather than removing it.

## What breaks with two replicas today

The code assumes one process in four places, each of which would misbehave on its own:

- **Ingress reaches every replica.** The console's `chat.console.*.in` and the three
  `chat.notify.*` subjects are core NATS subscriptions with no queue group, so each replica takes
  every frame and every notify: two turns per console message, two posts per notify. The
  Discord client opens an unsharded session per process, and each session gets every message.
- **The relay splits a task.** The `gateway-relay` durable hands deliveries to whichever replica
  pulls next, so one task's events land on both. Its render state (chunk assembly, steer counts,
  the rolling line, the console's result stash and cards-pending mark) is per process, and a split
  task renders wrong.
- **Session records are last-writer-wins.** Seventeen `Registry.Put` writers rely on a
  process-local lock per conversation. Two replicas writing one record lose each other's updates:
  an `ActiveTask`, a `PodName`, a delegation link.
- **The supervisor loops have no leader.** Reap, sweep and eviction run on every replica. Two can
  publish a supervisor terminal for one task, or delete a pod the other has just spawned and not
  yet recorded.

The HTTP doors (inject and A2A) keep their conversations in memory too, so a request and its
follow-up have to reach the same replica.

## Decision: active-standby first

**Run two replicas, one active.** A Kubernetes Lease elects the active replica, which does
everything the gateway does today. The standby runs, connects to nothing that takes work, and
takes over when the lease moves. Every assumption above stays true, because only one replica acts
at a time.

Active-active, where replicas share the work, would need every one of those assumptions
undone first: queue groups on the core subjects, task affinity on the relay, revision-checked
writes everywhere, leader-elected loops, and door state in KV or routed to its owner. That's a
rebuild of the gateway, for throughput nobody needs yet. It's the second phase below, and nothing
in the first phase blocks it.

### What only the active replica runs

On acquiring the lease, a replica starts what `Run` starts today: the relay subscription, the
chat backends (Slack Socket Mode, Discord, the Google Chat pull, the console and notify
subscriptions, the board route), the HTTP doors, and the reap and sweep loops. On losing it, it
stops them, in the order a shutdown stops them now: intake closes, the batches rendering finish,
and the relay gives back every event it holds unrendered (the relay's deferred-ack mode).

The standby holds a NATS connection, so it's warm, and nothing else. It doesn't open Slack's
socket: Slack would hand it events, and a standby can't take them.

### Handover

- **Graceful (a rollout, a drain).** On SIGTERM the active replica stops its work as above, then
  releases the lease. The standby, which has been retrying the lease, acquires it within its retry
  period and starts. Events the old replica gave back are redelivered at once, in order. The gap
  is the standby's retry period plus its start-up, about two seconds.
- **Crash.** The lease expires (`leaseDuration`, 15s), the standby takes it and starts. Events the
  dead replica held come back when the relay durable's AckWait (30s) runs out, since nothing gave
  them back. The relay can't redeliver sooner without a shorter AckWait, which costs more
  heartbeats; an open question below.
- **On the bus's side.** With the bus at three replicas, a stream or consumer leader election (a
  node drain, an upgrade's restart) can add its own few seconds to a redelivery. An ack sent during
  one can be lost, so an event already rendered comes back. The relay recognises it by its stream
  sequence and acks it without rendering again, the guard it already keeps for a crash between a
  render and its ack. A bus server draining in lame-duck mode spreads its clients' reconnects over
  about 30s: a burst of reconnects for the active gateway, not a gap.

### Fencing

A replica that can't renew the lease within `renewDeadline` (10s) stops all its work before the
lease can expire (15s), so two replicas never act on purpose at once. Clocks and pauses make that a
target, not a guarantee, so the records are fenced too: session-record writes become
revision-checked updates, retried on conflict. A stale replica's write then fails instead of
clobbering the new leader's. A side effect it made before the write, a post, can still happen
twice in the overlap. That's the same residue the relay already states for a crash between a post
and its record write.

### What a failover loses

Process memory doesn't move. Most of it is a cache or is rebuilt from the stream; what isn't:

- **Moved to KV in the build:** the console's cards-pending mark (already planned as its own
  change).
- **Lost, cosmetic:** the notifier's map of update keys to posted messages (an update after a
  failover posts anew instead of editing), the Slack board's per-row tap guard and last-sent
  blocks, the once-per-sender dropped-message notice, the Slack interaction state of a question
  someone was answering.
- **Lost, functional:** an HTTP door's in-memory conversation. A client that submitted to the old
  replica and polls the new one finds nothing. Inject is a dev and eval door, and an eval run
  rides out a rollout today by failing; the A2A door's clients see a task they can't read. An open
  question below.

### Reaching the active replica over HTTP

The inject and A2A doors' Services have no selector. Their endpoints are one EndpointSlice per
Service, which the operator creates empty, and the active gateway writes its own pod IP into it.
Readiness stays "healthy", not "active", so a rollout's new pod becomes Ready as a standby and the
rollout proceeds (the alternatives below say why routing can't ride on readiness).

The takeover order bounds when a request can reach the wrong replica:

1. The new leader acquires the Lease, then starts its work, doors included.
2. Only then does it write its pod IP into the EndpointSlice, replacing whatever was there.
3. A deposed leader stops serving door requests the moment it loses the Lease, or fails to renew
   it within `renewDeadline`, before its other work stops. A request that still reaches it
   before the EndpointSlice moves gets a 503 with `Retry-After`, never an answer from a replica
   that no longer owns the conversation.

So both replicas can be addressed only between step 1 and the old endpoint's removal in step 2,
and in that window the old one refuses rather than serves. kube-proxy picks up the change within
its own sync interval, typically a second or two.

If the EndpointSlice write fails, the active gateway keeps retrying with backoff while it holds
the Lease, and logs and counts each failure. The doors are unreachable meanwhile, and the
operator, which watches both, sets a `GatewayDoorsUnrouted` condition on the PlatformAgent while
the EndpointSlice doesn't name the Lease holder's pod. Chat traffic, the relay and the console
don't use these Services and keep working.

### Alternatives considered for routing

- **Readiness, or a readinessGate, true only on the active replica.** It deadlocks every
  rollout. A Deployment counts an unready pod as unavailable. With `maxUnavailable: 0` the new
  pod comes up as a standby and never goes Ready, so the old leader is never stopped. With
  `maxUnavailable: 1` the old leader is the only available pod and can't be taken either. A
  readinessGate counts toward Ready the same way, and setting it also needs `pods/status`.
- **A label the active replica sets on its own pod, selected by the Services.** It works, but it
  needs `patch` on every pod in the namespace (RBAC can't scope it to the gateway's own,
  generated, pod names), and the label can outlive the lease on a replica that dies mid-update.
- **The standby proxies door requests to the active replica** (address from the Lease holder). It
  needs no grant beyond the Lease, but it adds an application-level proxy, an extra hop, a second
  place for SSE streams to break, and a handover where the proxy itself is pointing at the old
  leader.

## Operator changes

- **Replicas from the CR,** default two on `next` installs, one allowed. Two is the floor for the
  point of this; more replicas add standbys and nothing else in phase one.
- **`RollingUpdate`** with `maxSurge: 1`, `maxUnavailable: 0`, replacing `Recreate`. The new pod
  comes up as a standby before the old one is told to stop.
- **A PodDisruptionBudget** of `maxUnavailable: 1`, so a node drain takes one replica at a time.
- **A zone topology spread** (`ScheduleAnyway`), as the callout and verifier already have, so a
  single-zone install still schedules.
- **The Lease, created by the operator** in the agent's namespace, under a fixed name. RBAC can't
  scope `create` to a name, so the operator owns creation, and the gateway's ServiceAccount gets
  only `get` and `update` on that one Lease (`resourceNames`).
- **The doors' EndpointSlices, created by the operator** (one per door Service, initially empty).
  The gateway's ServiceAccount gets `get` and `update` on those, by name, and nothing on pods.

## Build, as follow-ups

1. **Revision-checked session-record writes.** `Registry.Update`, a read-modify-write retried on
   conflict, replacing the seventeen `Put` callers. It's worth landing on its own: it also closes
   the races the sweep and the reap have with a spawn today.
2. **Leader election in the gateway.** Acquire, renew, step down; `Run`'s work gated on holding the
   lease; the shutdown order on stepping down; release on SIGTERM. Tests on an embedded server
   with two gateways: one acts, the other doesn't; the lease moves and the work follows.
3. **Door routing:** the selector-less door Services, the operator-created EndpointSlices and the
   `GatewayDoorsUnrouted` condition, and the gateway's write after it takes the Lease.
4. **Operator:** replicas, the strategy, the PDB, the spread and the Lease Role. Its tests pin
   each.
5. **The cards-pending mark on the record** (planned separately).
6. **Live runs** on a `next` lane: a rollout, a node drain and a force delete of the active pod
   while a task streams, measuring the gap and showing every event rendered once.

## Later: active-active

If throughput ever needs more than one active gateway, the prerequisites are known: queue groups
on the console and notify subjects; one relay consumer per replica over a sharded filter (by
conversation), so a task's events stay on one replica; the dedupe rings in KV; the supervisor
loops on their own lease; and the door conversations in KV. Revision-checked writes (step 1) are
the one piece phase one already lands.

## Open questions

- **The relay's AckWait on a crash.** 30s bounds how long a dead active replica's held events wait.
  A shorter AckWait shortens that, at the cost of more in-progress acks per held event. Worth
  measuring after the build.
- **The A2A door across a failover.** Its conversations could move to KV so a client can poll the
  new active replica, or the door can say plainly that a failover ends its in-flight requests.
- **The default replica count** on installs that don't set one.
