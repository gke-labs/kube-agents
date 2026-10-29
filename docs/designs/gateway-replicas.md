# Running the A2A gateway at more than one replica

## Purpose

The A2A gateway runs as one replica with a `Recreate` strategy, and that count is an invariant,
not a capacity setting (`buildA2AGatewayDeployment`). Every rollout, node drain or crash is a gap
with no gateway: chat turns aren't taken, task events aren't relayed, and the HTTP doors refuse.
This document decides how the gateway runs at more than one replica, in the order the build lands.

The goal is availability: a gateway that keeps answering through a rollout and survives losing a
node. It builds on the relay's delivery model (`spec-chatops-gateway.md`, the relay sections) and
on the bus going highly available (the NATS HA work: three servers, streams and buckets at three
replicas). Gateway HA without bus HA moves the single point of failure rather than removing it.

## What breaks with two replicas today

The code assumes one process in four places, each of which would misbehave on its own:

- **Ingress reaches every replica.** The console's `chat.console.*.in` and the three
  `chat.notify.*` subjects are core NATS subscriptions with no queue group, so each replica takes
  every frame and every notify: two turns per console message, two posts per notify. The Discord
  client opens an unsharded session per process, and each session gets every message.
- **The relay splits a task.** The `gateway-relay` durable hands deliveries to whichever replica
  pulls next, so one task's events land on both. Its render state (chunk assembly, steer counts,
  the rolling line, the console's result stash and cards-pending mark) is per process, and a split
  task renders wrong.
- **Session records are last-writer-wins.** Seventeen `Registry.Put` writers rely on a
  process-local lock per conversation. Two replicas writing one record lose each other's updates:
  an `ActiveTask`, a `PodName`, a delegation link.
- **The supervisor loops have no leader.** Reap, sweep and eviction run on every replica. Two can
  publish a supervisor terminal for one task, or delete a pod the other has just spawned and not
  yet recorded (`deleteIfUntracked` is safe today only because every spawn holds the in-process
  conversation lock).

The HTTP doors (inject and A2A) keep their conversations in memory too, so a request and its
follow-up have to reach the same replica.

## Decision: active-standby first

**Run two replicas, one active.** A Kubernetes Lease elects the active replica, which does
everything the gateway does today. The standby holds a NATS connection and answers its doors
with a 503 naming the holder (below). It does nothing else, and takes over when the lease moves. Every assumption above stays true, because only one replica acts at a
time.

Active-active, where replicas share the work, would need every one of those assumptions undone
first: queue groups on the core subjects, task affinity on the relay, fenced writes everywhere,
leader-elected loops, and door state in KV. That's a rebuild of the gateway. It's the second phase
below. Minting needs nothing either way: a task's capability is created once under its random id.

### What only the active replica runs

On acquiring the lease, a replica starts all of the gateway's work: what `Run` starts today (the
relay subscription, the chat adapters, the console and board routes, the HTTP doors, the reap and
sweep loops) and what `realMain` starts before it (the chat.notify notifiers, the Slack home
channel's learning, answer feedback). All of it moves behind the lease.

On losing the lease, or on SIGTERM, it stops in this order:

1. **Ingress first:** Slack's socket closes, Discord's session closes, the Google Chat pull and
   the core subscriptions (console, notify) stop, and the doors start answering 503. Slack
   spreads one app's events across its open sockets, so a socket left open on a deposed replica
   would take a share of messages it can no longer act on.
2. **The inbound queue:** turns already taken and not yet started are dropped, logged with their
   conversation. They were acked by their backend, so this is a loss, bounded by the queue's
   depth at that moment (see "What a failover loses").
3. **The relay:** the batches rendering finish, and the relay gives back every event it holds
   unrendered, in stream order. This needs the relay's deferred-ack mode, where an event is acked
   only after it renders. That's the relay ack-after-render change (#4183), which lands before the
   election; on `main` today the relay acks each event on receipt.
4. **The loops:** reap and sweep stop.

Then it releases the lease.

### Handover

- **Graceful (a rollout, a drain).** The active replica stops as above and releases the lease.
  The standby, which has been retrying the lease every `retryPeriod` (2s, jittered to between 2 and
  4.4s by client-go), acquires it and starts. Events the old replica gave back are redelivered at
  once, in order. The gap is the standby's next retry plus its start-up (Slack's `auth.test` and
  socket, Discord's identify, the relay's bind): a few seconds.
- **Crash.** The lease expires (`leaseDuration`, 15s) and the standby takes it. The dead replica's
  held relay events come back only when the relay durable's AckWait (30s) runs out, and a newer
  event the new leader rendered first would leave them rendering late and out of order, possibly
  after the task's terminal. So after a takeover from an expired lease, the new leader binds the
  relay only once AckWait has passed since the takeover. A held event's AckWait restarts at its
  last in-progress ack, which can come after the old leader's last renewal, but not after it died,
  and it died before the lease expired. NATS sends a consumer's pending redeliveries before new
  messages, so the dead leader's events come back first. Chat ingress starts at once; the relay
  waits 30s.
- **On the bus's side.** With the bus at three replicas, a stream or consumer leader election (a
  node drain, an upgrade's restart) can add its own few seconds to a redelivery. An ack sent during
  one can be lost, so an event already rendered comes back. The relay recognises it by its stream
  sequence and acks it without rendering again. That guard is part of the ack-after-render change:
  the session record keeps the sequences it rendered (`RelayedSeqs`, the last 256), in KV, so a new
  leader reads the same guard the old one wrote. A bus server draining in lame-duck mode spreads its clients' reconnects over
  about 30s: a burst of reconnects for the active gateway, not a gap.

### Fencing

A replica that can't renew the lease within `renewDeadline` (10s) steps down, and the stop has to
fit in the 5s before the lease can expire (`leaseDuration`, 15s). Ingress and the inbound queue
stop at once; the relay's render drain is bounded by that budget, and a batch still rendering when
it runs out is abandoned and given back. That makes two replicas acting at once unlikely, not
impossible: a paused process, or a clock that jumps, can still overlap the next leader.

So writes are fenced by the Lease's own epoch. `leaseTransitions` counts every acquisition, so
each leader's term has a number no earlier term shares. On acquiring the lease, and before it
starts any work, the new leader raises a term marker in the session-state bucket to its epoch.
Every record write, task-index write and session delete then checks two epochs: the marker's and
the stored record's. A write from an epoch older than either is refused, and the write is
conditional on the record revision it checked. So a deposed leader's write fails on every record
once the new term has started, including records the new leader hasn't touched yet. The marker
alone would leave a gap between reading it and writing, and the record's own epoch closes it for
any record the new leader has written.

The election's identity is the pod name plus a per-process id. client-go lets a process whose
identity matches the current holder renew without waiting and without counting a transition, so a
container restarting in the same pod under the bare pod name would carry on in the old term with
no new epoch. With the per-process id, a restarted container is a new holder: it waits out the
lease like any other and gets the next epoch.

The epoch fences records, not every side effect. A deposed leader in the overlap can still post
to a chat, publish a task or spawn a pod before it learns it lost. Its record write then fails, so
what it did isn't on record: a post may be duplicated, and a spawned pod is untracked, which the
new leader's sweep removes (the turn that spawned it is lost with it). That's the residue, and the
step-down budget keeps its window to seconds.

### What a failover loses

Process memory doesn't move, and the bus doesn't hold what nobody was subscribed to.

- **Ingress during the gap:** core NATS keeps nothing for a subject with no subscriber, so a
  console frame (including the MCP server's turns), and every `chat.notify` report-back from a
  card, published while no replica holds the subscription is lost. Discord replays nothing to a new
  session. Slack redelivers what wasn't acked, and the Google Chat pull waits in its queue. The gap
  is a few seconds on a graceful handover and 15s or more on a crash.
- **The inbound queue at step-down:** turns taken and not started (above).
- **Moved to KV in the build:** the console's cards-pending mark (already planned as its own
  change).
- **Lost, cosmetic:** the notifier's map of update keys to posted messages (an update after a
  failover posts anew instead of editing), the Slack board's per-row tap guard and last-sent
  blocks, the once-per-sender dropped-message notice, the Slack interaction state of a question
  someone was answering.
- **Lost, functional:** an HTTP door's in-memory conversation. A client that submitted to the old
  replica finds nothing on the new one. An open question below.

### Reaching the active replica over HTTP

The inject and A2A doors bind the pod's loopback, and their Services route nothing. They exist
so `kubectl port-forward svc/...` can resolve a pod. With two replicas, that can resolve the
standby. So:

- A standby's door answers every request with 503 and the active pod's name, which it reads from
  the Lease's `holderIdentity` (the part before the `_` and the per-process id), so a client knows
  where to port-forward instead.
- A client that has to reach the active replica port-forwards to `pod/<holder>`, read from the
  Lease (`kubectl get lease`). The eval harness and the repository's scripts do that.

No routing object moves on a handover, so none needs a grant. A port-forward to the old pod breaks
when that pod goes, and the client re-resolves the holder.

#### Keeping port-forwards smooth

Today people, scripts and the eval harness run `kubectl port-forward svc/<agent>-a2a-inject` (or
`-a2a-door`), and at two replicas that lands on the standby about half the time. Three things keep
that from being a trap:

- **The 503 says exactly what to run.** Its body names the holder and gives the command, for
  example `kubectl -n kubeagents-system port-forward pod/<holder> 8099`, so a person who hits the
  standby copies one line.
- **A one-liner and a helper resolve the holder.**
  `h=$(kubectl -n <ns> get lease <agent>-a2a-gateway -o jsonpath='{.spec.holderIdentity}'); kubectl -n <ns> port-forward pod/${h%%_*} <port>`
  does it by hand (a pod name has no `_`), and a small script in `hack/` wraps it, re-resolving when the port-forward drops
  on a handover.
- **Every caller is updated in the same change as the standby's 503:** the bench harness's
  inject transport (`bench/kube_agents_bench/harness.py` and `inject_transport.py`), which
  drives the required smoke gate, resolves the holder and re-resolves on a 503 or a dropped
  tunnel; `hack/ci-deploy.sh`, `hack/ci-eval-pr.sh` and `hack/rollback-roundtrip.sh`; and
  `bench/README.md` and the docs that show a door port-forward. That change lands before the
  operator renders a second replica, so the gate never sees a standby it can't route around.

Two surfaces aren't affected. The console page's port-forward goes to the console server, its own
Deployment, which proxies to the bus rather than to a gateway pod. The MCP server runs as its own
Deployment on the bus as the console principal. Both reach the active gateway through core NATS
subjects only the active replica subscribes to, so the one thing they see on a handover is the
ingress gap above.

### Alternatives considered

- **Electing through a NATS KV key** instead of a Kubernetes Lease. It needs no Kubernetes grant,
  and its revision would be an epoch. It lost because the doors' clients reach the active pod
  with `kubectl`, which can read a Lease and not a bus key, and a Lease already counts its
  transitions.
- **Readiness true only on the active replica,** to route by Service. It deadlocks every
  rollout: a Deployment counts an unready pod as unavailable, so with `maxUnavailable: 0` the new
  pod never goes Ready as a standby and the old leader is never stopped, and with
  `maxUnavailable: 1` the old leader is the only available pod and can't be taken either. A
  readinessGate counts toward Ready the same way. And the doors aren't reached through Service
  routing anyway.
- **A label the active replica sets on its own pod.** It needs `patch` on every pod in the
  namespace, which the gateway's Role refuses on purpose (a pod's image and labels are mutable, and
  the sweep relies on the gateway holding no pod patch). The label also outlives a container that
  restarts in the same pod.
- **A selector-less Service with an EndpointSlice the gateway updates.** It routes nothing for a
  door bound to loopback, and `kubectl port-forward svc/...` refuses a Service with no selector.
- **The standby proxying door requests to the active replica.** It adds an application-level
  proxy, an extra hop and a second place for SSE streams to break, where a 503 naming the holder
  costs the client one retry.

## Operator changes

- **Replicas from the CR,** default two on `next` installs, one allowed. More replicas add
  standbys and nothing else in phase one.
- **A readiness probe** that holds the pod not Ready until it has a NATS connection and has read
  the Lease. Readiness means "healthy", active or not, so a rollout's new pod becomes Ready as a
  standby.
- **`RollingUpdate` with `maxSurge: 1`, `maxUnavailable: 0`,** replacing `Recreate`, so the new
  pod comes up as a standby before an old one stops. A surge pod needs room under the agent
  namespace's quota; where there's none, the rollout waits, which is the reason `Recreate` was
  chosen. The operator renders `maxSurge: 0`, `maxUnavailable: 1` instead when the CR asks for one
  replica. The ReplicaSet may stop the active pod before the standby, which costs a second, graceful
  handover in one rollout; steering that (the pod-deletion-cost annotation) would need a pod
  patch, so it isn't done.
- **A PodDisruptionBudget** of `maxUnavailable: 1`, with `unhealthyPodEvictionPolicy:
AlwaysAllow` as the callout's has, so a node drain takes one replica at a time and an unhealthy
  replica never blocks a drain.
- **A zone topology spread** (`ScheduleAnyway`), as the callout and verifier already have, so a
  single-zone install still schedules. On Autopilot the spread and the PDB behave as on Standard:
  Autopilot honours a PDB on its own node upgrades, and `ScheduleAnyway` asks for no capacity it
  can't provide.
- **The Lease, created by the operator** in the agent's namespace, under a fixed name. RBAC can't
  scope `create` to a name, so the operator owns creation, and the gateway's ServiceAccount gets
  only `get` and `update` on that one Lease (`resourceNames`). Nothing else changes in its Role:
  no EndpointSlice grant and no pod patch. The project's tech lead approved this grant for phase
  one.

## Build, as follow-ups

1. **Fenced session-record writes.** `Registry.Put` becomes a write conditional on the revision it
   checked, refusing a write from an epoch older than the term marker's or the stored record's.
   The task index and session deletes take the same check. The seventeen callers don't change,
   and within one term the last write still wins, as it does with one active replica today. The
   registry gains the call that raises the marker, which the election makes. Until the election
   exists, every write carries epoch 0.
2. **Leader election in the gateway.** It lands after the relay's ack-after-render change.
   Acquire (identity: pod name and per-process id), raise the term marker, renew and step down; all of `Run`'s and
   `realMain`'s work gated on the lease; the step-down order and budget above; the relay's delayed
   bind after a takeover from an expired lease; release on SIGTERM. Tests on an embedded server
   with two gateways: one acts, the other doesn't; the lease moves and the work follows; a crash
   takeover renders the dead leader's held events before newer ones.
3. **The standby's doors:** 503 with the holder and the exact port-forward command, the `hack/`
   helper, and the eval harness, the CI scripts and the docs resolving the holder. It lands before
   step 4, so nothing meets a standby it can't route around.
4. **Operator:** replicas, the readiness probe, the strategy, the PDB, the spread, the Lease and
   its Role. Its tests pin each.
5. **The cards-pending mark on the record** (planned separately).
6. **Live runs** on a `next` lane: a rollout, a node drain and a force delete of the active pod
   while a task streams, measuring the gap and showing every event rendered once.

## Later: active-active

Phase 1 is the plan for now. Scaling may change that, so nothing in phase 1 makes phase 2 harder:
state that more than one replica would share goes to KV, not into the active replica's memory, and
new code doesn't assume it's the only instance. A per-process cache that a replica can rebuild is
fine; state another replica would need isn't.

If throughput ever needs more than one active gateway, the prerequisites are known: queue groups
on the console and notify subjects; one relay consumer per replica over a sharded filter (by
conversation), so a task's events stay on one replica, since the relay's deferred-ack model assumes
one consumer per durable; the dedupe rings in KV; the supervisor loops on their own lease; and the
door conversations in KV. Fenced writes (step 1) are the one piece phase one already lands.

## Open questions

- **The relay's AckWait.** 30s bounds both how long a dead active replica's held events wait and
  how long the relay waits after a crash takeover. A shorter AckWait shortens both, at the cost of
  more in-progress acks per held event. Worth measuring after the build.
- **The A2A door across a failover.** Its conversations could move to KV so a client can poll the
  new active replica, or the door can say plainly that a failover ends its in-flight requests.
