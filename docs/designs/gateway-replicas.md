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
   only after it renders, which the relay ack-after-render change (#4183) put on `main`.
4. **The loops:** reap and sweep stop.

Then it releases the lease, if a fresh read still names it as the holder. A gateway whose term
followed a crash and is ending inside the relay's AckWait leaves the lease to expire instead: the
dead holder's events may still be pending, and a lease that still names a holder makes the next
leader hold its relay too.

### Handover

- **Graceful (a rollout, a drain).** The active replica stops as above and releases the lease.
  The standby, which has been retrying the lease every `retryPeriod` (2s, jittered to between 2 and
  4.4s by client-go), acquires it and starts. Events the old replica gave back are redelivered as
  soon as the standby's relay binds, without waiting out AckWait, but not always in order: the
  give-back keeps stream order only for a relay already bound when it falls due, milliseconds
  after the stop, and with none bound the bus can move the oldest event behind later ones. The relay
  sorts each batch by stream sequence, which restores the order only among events that land in
  one batch. The gap is the standby's next retry plus its start-up (Slack's `auth.test` and
  socket, Discord's identify, the relay's bind): a few seconds.
- **Crash.** The lease expires (`leaseDuration`, 20s) and the standby takes it. The dead replica's
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

### What it costs

The relay waits 30s after any start that follows a crash: the dead process's held events come back
only when their AckWait runs out, and they have to render before newer ones. That holds for a
standby taking over an expired lease and for a container restarting in its own pod, with one
replica or two. Chat ingress starts at once either way. A container restarting in its own pod
re-takes the lease at once, since its identity is the pod name (below), so that crash, the most
common one, recovers as fast as it does today. What the second replica adds is a gateway during
the events where one replica has none until a new pod schedules and starts: a rollout or a node
drain (a graceful handover in a few seconds) and a node or zone loss (the lease's 20s, then the
relay's 30s).

The election also makes the gateway depend on the API server, which it otherwise needs only for
side tasks (spawning session pods, the home-channel record). A leader whose renewals fail gives up
10s after the first failure (about 12s after its last good renewal), stops and exits, and no
replica can take the lease until the API server answers again. So any control-plane outage longer
than that stops chat, the console and the relay for its length. A zonal cluster's control-plane
upgrade is the common case: several minutes with no API server, which an unelected gateway rides
through. Two replicas don't change this, since both need the API server. This is accepted: an
install that wants the gateway highly available runs a regional control plane, which keeps the API
server up through an upgrade.

### Fencing

A replica that can't renew the lease steps down, and the stop has to fit before a standby can
take the lease. In client-go the renew loop starts its `renewDeadline` (10s) window one
`retryPeriod` (2s) after the last good renewal, so a leader that stops renewing gives up about 12s
after it, and a standby that saw that renewal can take the lease after `leaseDuration`. With
client-go's 15s default that leaves about 3s, so the gateway sets `leaseDuration` to 20s, leaving
about 8s. Ingress and the inbound queue stop at once. The relay's render drain is bounded at 5s
inside that, and a batch still rendering when it runs out is abandoned and given back. The relay's
own stop drain (`relayStopDrainTimeout`) is 15s today, sized to the pod's grace period on SIGTERM.
So build step 2 (below) gives the lost-lease path its own 5s bound, and SIGTERM keeps the 15s.
That makes two replicas acting at once unlikely, not impossible: a paused process, or a clock that
jumps, can still overlap the next leader.

So writes are fenced by a term number. The term isn't the Lease's `leaseTransitions`: the Lease
can be deleted and recreated (by hand, or by a reinstall or restore that keeps the bus volume),
and its count would start again under records stamped with later numbers. Instead the term is a
counter in the session-state bucket, beside the records it fences. On acquiring the lease, and
before it starts any work, the new leader increments the counter and takes the result as its
term. Every record write, task-index write and session delete checks two terms: the counter's and
the stored record's. A write from a term older than either is refused, and the write is
conditional on the record revision it checked.

That fences most of a deposed leader's writes, not all of them. Once the new term has started, a
deposed write is refused on any record the new leader has written. It's also refused on every
other record, unless the deposed leader read the counter before the new term began and then
paused before writing. The counter and the record are separate keys, and nothing makes the pair
atomic. A write that slips through that gap lands on a record the new leader hasn't written
yet. The new leader's own next write to that record is conditional on its revision, so it
re-reads the record and carries on from what the deposed leader left.

The task index widens that gap a little. A task-index write is fenced by its session record's term,
but the index is a third key, so the write isn't conditional on the record's revision: a deposed
index write can land even after the new leader has written the session record. And the counter
itself can be broken by hand. Deleted, or set below the terms the records carry, it would let terms
restart beneath them, so the gateway refuses to start a term over a deleted counter and refuses
every write to a record whose term is above its own, naming the counter in both errors. Either needs
a hand fix: the counter set to at least the highest term a record carries.

A write the fence refuses means a newer term has started, so the gateway steps down there, as it
does on a lost lease, without waiting to see the lease move. The exceptions are a write refused by
a record above a counter reset by hand, and one whose counter can't be read again to tell a reset
from a new term: no newer term is known to exist there, and stepping down would only hand the same
refusal to the next replica.

The election's identity is the pod name. client-go lets a process whose identity matches the
current holder renew without waiting, so a container restarting in its own pod re-takes the lease
at once. It still starts a new term: a gateway increments the term counter every time it starts
leading, whether client-go counted a transition or not, so the restarted process writes under a
term the dead one never had. (Two containers never share a pod's name at once, so the identity
can't be held twice.) A restarted container that re-takes the lease this way still waits AckWait
before binding the relay, as any start after a crash does.

The term fences records, not every side effect. A deposed leader in the overlap can still post
to a chat, publish a task or spawn a pod before it learns it lost. Its record write then usually
fails, so what it did isn't on record: a post may be duplicated, and a spawned pod is untracked, which the
new leader's sweep removes (the turn that spawned it is lost with it). That's the residue, and the
step-down budget keeps its window to seconds.

### What a failover loses

Process memory doesn't move, and the bus doesn't hold what nobody was subscribed to.

- **Ingress during the gap:** core NATS keeps nothing for a subject with no subscriber, so a
  console frame (including the MCP server's turns), and every `chat.notify` report-back from a
  card, published while no replica holds the subscription is lost. Discord replays nothing to a new
  session. Slack redelivers what wasn't acked, and the Google Chat pull waits in its queue. The gap
  is a few seconds on a graceful handover and 20s or more on a crash.
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
  the Lease's `holderIdentity`, so a client knows where to port-forward instead.
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
  `kubectl -n <ns> port-forward pod/$(kubectl -n <ns> get lease <agent>-a2a-gateway -o jsonpath='{.spec.holderIdentity}') <port>`
  does it by hand, and a small script in `hack/` wraps it, re-resolving when the port-forward
  drops on a handover.
- **Every caller is updated in the same change as the standby's 503:** the bench harness's
  inject transport (`bench/kube_agents_bench/harness.py` and `inject_transport.py`), which
  drives the required smoke gate, resolves the holder and re-resolves on a 503 or a dropped
  tunnel; `hack/rollback-roundtrip.sh`, the one script that port-forwards a door; and
  `bench/README.md` and the docs that show a door port-forward. That change lands before the
  operator renders a second replica, so the gate never sees a standby it can't route around.

Two surfaces aren't affected. The console page's port-forward goes to the console server, its own
Deployment, which proxies to the bus rather than to a gateway pod. The MCP server runs as its own
Deployment on the bus as the console principal. Both reach the active gateway through core NATS
subjects only the active replica subscribes to, so the one thing they see on a handover is the
ingress gap above.

### Alternatives considered

- **Electing through a NATS KV key** instead of a Kubernetes Lease. It needs no Kubernetes grant,
  and the term would live with the records. It lost because the doors' clients reach the active
  pod with `kubectl`, which can read a Lease and not a bus key. The term counter is in the bus
  anyway, so the Lease only elects.
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

- **Replicas from the CR** (`spec.deployment.availability.a2aGateway.replicas`), default two on
  `next` installs, one allowed. More replicas add
  standbys and nothing else in phase one.
- **A readiness probe** that holds the pod not Ready until it has a NATS connection and has read
  the Lease. Readiness means "healthy", active or not, so a rollout's new pod becomes Ready as a
  standby.
- **`RollingUpdate` with `maxSurge: 1`, `maxUnavailable: 0`,** replacing `Recreate`, so the new
  pod comes up as a standby before an old one stops. The operator renders it, and a second
  replica, only once it knows the gateway image runs the election, because a gateway from before
  the election holds no Lease and would act beside an elected one, and term 0 fences nothing. The
  operator records an election as a `GatewayElected` condition on the PlatformAgent, naming the image of the
  pod that holds the Lease (the holder is a pod name, and the operator reads the pod). It renders two
  replicas when that condition names the image it now renders, or when the condition is set for
  any image and the new one comes from the operator's own release (the operator image's tag, the
  second rung of `a2aReleaseImage`): an elected gateway is running, and the operator's release
  runs the election by construction. Anything else first rolls as one replica with `Recreate` (an
  `A2A_GATEWAY_IMAGE` pin, a tag taken from `PLATFORM_AGENT_IMAGE`, or the first roll of any image
  when no gateway has been elected yet), and moves to two once a pod on that image holds the
  Lease. The condition describes the gateway that's running, not one that once ran: the operator
  clears it whenever the gateway Deployment rolls to an image outside the operator's own release, and when
  the mode goes back to today, as `BusProvisioned` is. So a pin to a pre-election image clears it,
  and removing the pin rolls once at one replica again. A deleted Lease or its held-back recreate
  doesn't change the running image, so it leaves the condition, and the two replicas, in place. A status condition doesn't
  survive most restores, so a restored install starts at one replica until a
  gateway takes the Lease again. The condition can outlive the gateway it describes when an older
  operator, which doesn't know it, runs the gateway unelected in between. So the operator scales out
  from one replica only on an election it reads that pass, and uses a recorded condition only to
  keep a gateway already rolling with the election's env. A CI install, which always pins the image, spends its first roll
  at one replica and runs two after that. A gateway from before the election never takes the
  Lease, so a pin to one stays at one replica. The same check covers an install upgrading across
  the election in one roll: its old gateway holds no
  Lease, so that roll is one replica and `Recreate`, and two follow once the elected gateway has
  taken it. A surge pod needs room under the agent
  namespace's quota; where there's none, the rollout waits, which is the reason `Recreate` was
  chosen. The operator renders `maxSurge: 0`, `maxUnavailable: 1` instead when the CR asks for one
  replica. The ReplicaSet may stop the active pod before the standby, which costs a second, graceful
  handover in one rollout; steering that (the pod-deletion-cost annotation) would need a pod
  patch, so it isn't done.
- **A PodDisruptionBudget** of `maxUnavailable: 1`, with `unhealthyPodEvictionPolicy:
AlwaysAllow` as the callout's has, so a node drain takes one replica at a time and an unhealthy
  replica never blocks a drain. It exists only while more than one replica is Ready: one
  applied while the standby can't come up (no room, a failed pull) would pin the only pod.
- **A zone topology spread** (`ScheduleAnyway`), as the callout and verifier already have, so a
  single-zone install still schedules. On Autopilot the spread and the PDB behave as on Standard:
  Autopilot honours a PDB on its own node upgrades, and `ScheduleAnyway` asks for no capacity it
  can't provide.
- **The Lease, created by the operator** in the agent's namespace, under a fixed name. RBAC can't
  scope `create` to a name, so the operator owns creation, and the gateway's ServiceAccount gets
  only `get` and `update` on that one Lease (`resourceNames`). Nothing else changes in its Role:
  no EndpointSlice grant and no pod patch. The project's tech lead approved this grant for phase
  one. Because the gateway can't recreate the Lease, the operator watches it: the Lease carries
  the PlatformAgent's owner reference and the controller `Owns` it. The operator's ClusterRole
  already holds every Lease verb this needs. It recreates a deleted Lease one `leaseDuration`
  after it saw the deletion, not at once. A Lease recreated with no holder can be taken on a
  standby's next retry, and the old leader would keep acting until its renewal gives up, so the
  wait lets the old leader step down first. While the Lease is absent, both replicas answer their
  doors with a 503 saying no replica holds the lease, and the next acquisition starts a new term
  as usual. The gap is about 20s, for an event that only a hand or a restore causes.

## Build, as follow-ups

1. **Fenced session-record writes.** `Registry.Put` becomes a write conditional on the revision it
   checked, refusing a write from a term older than the term counter's or the stored record's.
   The task index and session deletes take the same check. The seventeen callers don't change,
   and within one term the last write still wins, as it does with one active replica today. The
   registry gains the call that starts a term, which the election makes. Until the election
   exists, every write carries term 0, and term 0 fences nothing.
2. **Leader election in the gateway.** Acquire (identity: the pod name), start a term on every
   acquisition and every start that re-takes its own lease, renew and step down; all of `Run`'s and
   `realMain`'s work gated on the lease; the step-down order and budget above, with the relay's
   drain bounded at 5s on the lost-lease path; the relay's delayed
   bind after any start that follows a crash, a takeover from an expired lease or a same-pod
   restart; release on SIGTERM; and stepping down on a write the fence refuses. A replica that
   stops leading exits, and its restarted container waits as a standby. The election runs only
   when the gateway is given its Lease (`A2A_GATEWAY_LEASE`, with `POD_NAME` as the identity),
   which step 4 renders with the Role; without it the gateway runs unelected at term 0, as
   before. Tests on an embedded server
   with two gateways: one acts, the other doesn't; the lease moves and the work follows; a crash
   takeover renders the dead leader's held events before newer ones.
   Rolling back from this step needs nothing: a gateway without the election writes at term 0,
   which fences nothing, so step 1's build and anything older still write after terms have
   started.
3. **The standby's doors:** 503 with the holder and the exact port-forward command, the `hack/`
   helper, and the eval harness, the CI scripts and the docs resolving the holder. It lands before
   step 4, so nothing meets a standby it can't route around.
4. **Operator:** replicas, the readiness probe, the strategy (held at one replica and `Recreate`
   until `GatewayElected` names the running image, for the first roll ever and for any image
   outside the operator's own release; the condition cleared on a roll to any image outside it), the
   PDB, the spread, the Lease (with its owner reference and
   an `Owns` watch, recreated one `leaseDuration` after a deletion) and its Role. Its tests pin
   each, including the held-back recreate, the first roll across the election, and a pin to a
   pre-election image and back. It lands in two parts: first the Lease, its Role grant and the
   gateway's election env, at one replica with `Recreate`, behind an operator flag
   (`A2A_GATEWAY_ELECTION`) that CI and the live runs turn on; then the flag on by default
   (`false` turns it off), the replicas, the strategy and
   their guard, the readiness probe, the PDB and the spread.
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
