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

The doors' Services select a label the active replica sets on its own pod when it acquires the
lease, and clears when it steps down. A standby's door is never routed to. Readiness stays
"healthy", not "active", so a rollout's new pod becomes Ready as a standby and the rollout
proceeds.

## Operator changes

- **Replicas from the CR,** default two on `next` installs, one allowed. Two is the floor for the
  point of this; more replicas add standbys and nothing else in phase one.
- **`RollingUpdate`** with `maxSurge: 1`, `maxUnavailable: 0`, replacing `Recreate`. The new pod
  comes up as a standby before the old one is told to stop.
- **A PodDisruptionBudget** of `maxUnavailable: 1`, so a node drain takes one replica at a time.
- **A zone topology spread** (`ScheduleAnyway`), as the callout and verifier already have, so a
  single-zone install still schedules.
- **RBAC for the gateway's ServiceAccount:** get, create and update on its one Lease by name; and
  patch on pods in its namespace, for the active label. Both are new grants.

## Build, as follow-ups

1. **Revision-checked session-record writes.** `Registry.Update`, a read-modify-write retried on
   conflict, replacing the seventeen `Put` callers. It's worth landing on its own: it also closes
   the races the sweep and the reap have with a spawn today.
2. **Leader election in the gateway.** Acquire, renew, step down; `Run`'s work gated on holding the
   lease; the shutdown order on stepping down; release on SIGTERM. Tests on an embedded server
   with two gateways: one acts, the other doesn't; the lease moves and the work follows.
3. **The active label and the door Services' selector,** with the pod-patch grant.
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
