# NATS deployment spec

- **Author:** [@bnaylor]
- **Date:** 2026-08-24
- **Status:** draft, for review
- **Companion:** the A2A payload spec (`spec-a2a-payloads.md`) - owns subjects and message shape

## Purpose

This spec covers the NATS deployment for the A2A fabric: JetStream stream and retention
layout, accounts and connection-time authorization, how the durable stream surfaces as the
audit substrate, and the client resilience contract the stage 1 client library must satisfy.

Subject taxonomy and message shape belong to the A2A payload spec. Both docs landed the
same day, so the planned tie-break rule never fired; the call (8/24) is that the payload
spec's layout wins and this doc binds to its subjects and topic classes.

## What we deploy

NATS 2.10 or later (auth callout requires it), JetStream enabled, file storage on a PV.
The operator renders the whole deployment from the mode switch: `mode: next` gets a NATS
cluster, the default gets nothing. Dark until promoted.

Production guidance is a 3-node cluster with stream replicas R3. Dev and CI run a single
node with R1 under the same config surface - the conformance suite runs against `kind`, so
nothing here may depend on a managed control plane.

**The customer operates this.** Server restarts - rollouts, node drains, PV failover - are
routine operations, not incidents. That fact drives the resilience contract below more than
any other input.

## Streams and retention

**One retention rule before any layout: acknowledgement must not delete.** The durable
stream is the audit substrate for inter-agent traffic, so a message's lifetime is the audit
window, not the delivery lifecycle. Concretely:

- All message streams use **limits-based retention** with an age window. Interest and
  workqueue retention are ruled out - both delete on ack, which destroys replay exactly when
  you want it (after the interaction completed and something looks wrong.)
- Consumers are **durable push with explicit ack** - the downstream can ack, so
  server-tracked delivery is correct. `MaxAckPending` is the back-pressure valve, set per
  stream class.
- Replay is a read, never a consume. Nothing an auditor does can change delivery state.

Streams bind to the payload spec's subjects and topic classes:

| Stream           | Subjects             | Retention                                   | Consumers                                                                                                                                                                                                                                                                                                                |
| :--------------- | :------------------- | :------------------------------------------ | :----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `TASKS`          | `a2a.tasks.>`        | Age window W (72h dev default), R3          | One durable per profile, held by the dispatcher, on `a2a.tasks.{profile}.*.in`, `MaxAckPending` ~50 per profile - per-profile consumers so one capped or crash-looping profile's unacked backlog cannot head-of-line block another profile's dispatch. `tasks/get` is an ephemeral replay of `…events`, never a consume. |
| `DIRECTORY`      | `a2a.agents.>`       | `max_msgs_per_subject: 1`, R3               | Last-value; the tombstone replaces the card                                                                                                                                                                                                                                                                              |
| `TOPICS-STATE`   | state-class topics   | `max_msgs_per_subject: 8`, no age limit, R3 | Read latest-per-subject                                                                                                                                                                                                                                                                                                  |
| `TOPICS-JOURNAL` | journal-class topics | `max_age: 30d`, R3                          |                                                                                                                                                                                                                                                                                                                          |

State and journal topics both live under `a2a.topics.>`, so the two topic streams' subject
lists are rendered per topic from the provisioned registry - which topics exist is already
config, and the retention class lives there too. Shared fan-out (blueprints, config
availability) is the shared topics; recipients that must confirm receipt get durable
consumers on the topic streams. Heartbeats (`agents.hb.>`) are core NATS, outside
JetStream, per the payload spec.

Everything is R3 in production. Status and artifact events are the bulk of the volume and
the tempting place for a cheaper R1 class, but they are also the replay and audit record,
and R1 loses them to a single node failure. Audit wins. The cost knob is W, not replicas.

Every stream also carries a hard `max_bytes` cap with `discard: old` alongside its age
limit (dev defaults: 20GiB `TASKS`, 5GiB `TOPICS-JOURNAL`, 1GiB each for `TOPICS-STATE`
and `DIRECTORY`). A runaway telemetry flood drops oldest chunks early; it never fills
the PV and stalls the whole JetStream deployment, which would take `runtime-state`,
`session-state`, and discovery down with it. The honest cost: under byte pressure,
replay completeness degrades oldest-first - a running task's early events can age out of
a flooded stream - and the byte-headroom alert below exists so that state is paged on
before it is reached.

W is TBD - see Open questions. It is not just a cost knob; see the audit section.

Three KV buckets ride the same JetStream deployment:

- `runtime-state` - which agents are alive, what is in flight. Runtime state does not
  belong in git, and this is where it goes instead.
- `session-state` - the gateway's session registry (session key, `contextId`, current
  pod, roster, and the active task's bounded ask echo - user content, deliberately; the
  gateway design owns the content-vs-identifier rule). The gateway's user is the only
  writer by grant, and since the 8/31 narrowing the only reader too: no other user
  holds `$KV.session-state.>` on either side, and the web user's consumer-create route
  INTO the bucket - a push consumer bound to `KV_session-state` - is closed by the
  per-stream enumeration. One write route survives and is the deliver-subject residue
  recorded with the web user below, not an exception to it: a consumer created on a
  granted stream may aim its deliver subject at `$KV.session-state.>`, and the server's
  own replay publishes land as bucket revisions. What the gateway reads back is
  corruptible that way - current pod name, bus session name, roster - so "only writer"
  is a statement about grants, not a property the subject list enforces. The residue's
  closes are the residue paragraph's: the callout, or a separate account with an
  export/import.
- A bucket reserved for capability entries per the capability envelope design
  (`docs/architecture/09-capability-envelope.md`), which landed on KV-backed
  capabilities. Reserved so the account layout allows for it; it arms with the
  authority work.

## Accounts and connection-time authorization

The property this section exists to preserve: **the bus decides who may say what before a
message is read.** Publish and subscribe permissions are checked when the connection is
established. A compromised or prompt-injected agent cannot emit on a subject it has no
claim to, because the connection has no such right - no application code is consulted.

**Authentication is auth callout, not decentralized JWT** - no operator/account key
hierarchy to manage. One signing key does exist, and it is the most powerful thing in
this deployment: the callout issuer key that signs each authorization response, whose
holder can mint arbitrary bus users. The capability envelope design
(`docs/architecture/09-capability-envelope.md`, "one cryptographic key does exist") owns
that analysis; the callout service inherits its hardening requirements. The callout
validates the client's KSA token against the cluster's OIDC issuer (audience-bound,
short-lived, kubelet-rotated) and returns the account and the permission set. Revocation
is the issuer's problem, and it already solved it. This works on stock Kubernetes; there
is no GKE dependency. Concretely, validation is a `TokenReview` call against the local
API server - zero key handling, works on any conformant cluster - with local JWT
verification against the API server's `openid/v1/jwks` endpoint as the offline
alternative.

Status (amended 9/4, the callout armed): the render now carries the `auth_callout`
block, and a principal authenticates one of two ways. **Through the callout**, by
presenting a projected ServiceAccount token: the bus provisioning Job, and nothing else
yet. **Statically**, from `nats.conf` and listed in `auth_users`: the callout itself,
which cannot authenticate through the thing it is; the chatops gateway, purely as
sequencing, since it has a ServiceAccount and its client program lands separately from
this render; the shared `worker`, which is also what the platform agent pod and the
Hermes bridge sidecar beside it connect as, because a session pod carries no Kubernetes
identity for the callout to resolve and the bridge has not moved off the shared
credential; `web`, because a browser never can; `seed`, because the hand-applied seed
tooling is applied rather than rendered and dropping its user would refuse an object
already running; and `sys`, a human
at a port-forward.

Three of those are permanent - the callout, which cannot authenticate through itself;
`web`, because a browser never can; and `sys`, which is a human rather than a workload -
and the rest are waiting on something nameable. The single
source for all of it - the config's static user blocks, the callout's map, and the
`NATS_USER` a client is handed so it can set its inbox prefix - is
`platformagent_a2a_identities.go`; before the callout those three lived in a config
string, a Secret and a container env block with nothing but review connecting them.

Those credentials belong in Secret data and nowhere else in the render: no rendered
object name, label, or annotation may carry a password or a digest of one, truncated or
not. Names and labels are readable by anything that can list the namespace, so a digest
there is an offline target the day a password is hand-set. That binds every renderer of
this stack, not one function - the rollout hash on the NATS pod template is over the
config rendered with placeholders in the passwords' place plus the credentials Secret's
`resourceVersion`, which is what lets a config change and a rotation both roll the bus
without a credential reaching the digest.

Layout:

- **`$SYS`** - human operators and monitoring only. No agent ever authenticates into it.
- **`AUTH`** - the auth callout service and nothing else, and the isolation is a boundary
  rather than tidiness. The server publishes every authorization request into this account
  and takes the first answer that returns on the reply inbox, and it does **not** check
  that the answer's outer envelope was signed by the configured issuer (measured; the
  inner user JWT's signature is checked). So anything able to publish into this account's
  `$SYS._INBOX.>` and win the race can refuse an authorization - it could not forge a
  grant without the issuer seed, but it could deny one. Nothing else belongs in here.
- **One application account per scope.** The account is the tenant boundary and the blast
  radius container. Stage 1 exercises exactly one; the multi-scope split (and any
  cross-account exports) is designed but deliberately unexercised until a second scope
  exists.
- **One user per agent identity** inside the account. Permissions are exact subject
  lists, deny by default: publish only to the subjects its role emits on, subscribe only
  to its own addressee prefix on the task subjects (`a2a.tasks.<its name>.>`) plus the
  shared topics it is granted. The addressee token in the task subjects (payload spec
  0.4) is what makes these grants expressible - executor-granularity at connect time,
  with per-task scoping the parked tightening under the authority work.
- **The JetStream tax.** Deny-by-default reaches JetStream's own plumbing, and three
  grants are part of being a JetStream client at all: the `$JS.API` subjects a role's
  streams and buckets need, enumerated per stream and per verb where the caller set is
  known (the worker's list is the operator's `a2aWorkerJetStreamGrants`; a user still
  holding `$JS.API.>` holds playground posture); `$JS.ACK.<its streams>.>` for explicit
  acks - an ack is a publish, and missing this grant means every consumer redelivers
  forever while TCP health stays green, the NR-5 incident class created at connect time;
  and `$JS.FC.>` for flow control. The inbox rule cuts both ways, too: a client whose subscribe grant
  is `_INBOX.<user>.>` MUST configure its inbox prefix to match - the client library's
  default random inbox is refused by the user's own grant and every API call times out.
  Both halves were found live (8/26): the provision Job could never succeed and no
  consumer could ever ack until these landed. The ack grant should be scoped per
  stream, for the reason the web section below teaches: an ack subject names a stream
  and a consumer, never the caller, so an unscoped `$JS.ACK.>` lets any holder `+TERM`
  another principal's in-flight delivery. That narrowing has landed: every rendered
  principal's ack grant is scoped to the streams it consumes with explicit ack -
  `$JS.ACK.TASKS.>` for the gateway and the shared worker - and the principals whose reads
  are ordered or ack-none hold no ack grant at all. What scoping still cannot express is
  per-consumer scope inside a granted stream, since NATS wildcards match whole tokens.
- **Topic publish grants are exact, never namespace wildcards.** Publish grants match
  the provisioned topic list subject-for-subject. A wildcard over a topic namespace
  turns provisioned-only into silent loss - a publish to an unprovisioned topic sails
  into core NATS and vanishes; an exact grant makes it a connect-time refusal. The
  corollary is operational: adding a topic is two edits that must travel together, the
  stream's subject list and the writer's grant. The rationale is publish-side only: a
  read-side wildcard (`a2a.topics.>` in a subscribe list) loses nothing and stays fine.
- **Per-user inbox prefixes.** Push delivery uses inbox subjects, so each user gets its own
  prefix (`_INBOX.<user>.>`) and permission to subscribe only to that. Without this, any
  agent can subscribe to any inbox and the whole property above leaks through the reply
  path.
- **The web read surface (amended 8/31; rewritten the same day after review).** One
  `web` user for the read-only web UI, and the only bus credential that is published to
  a browser by design. Subscribe on `a2a.>` and its own inbox; publish only the JetStream
  read API - account-level `INFO`, and `STREAM.INFO`, `CONSUMER.CREATE`, `CONSUMER.INFO`,
  `CONSUMER.MSG.NEXT` **enumerated per stream** over the four message streams - plus its
  own inbox. It rides a websocket listener on 9222 rendered plain (`no_tls: true`) with
  an `allowed_origins` allow-list.

  **"Read-only" is not expressible as a subject list, and the first version of this user
  proved it.** Subject permissions cannot see a request BODY, and JetStream puts the
  reach there - a consumer's target stream, its durability and its delivery subject are
  all fields. Reproduced live against the rendered config before the narrowing:
  `$JS.API.CONSUMER.CREATE.>` let `web` build a push consumer on
  `KV_session-state` delivering into its own inbox and read the session registry out of a
  bucket it has no `$KV` grant for (subscribe permissions are not consulted at consumer
  creation; the deliver subject is); `$JS.ACK.>` let it publish `+TERM` onto the
  gateway's in-flight delivery, because an ack subject names a stream and a consumer and
  never the caller. The consumer-create escape is closed by the enumeration; the ack
  escape by deleting the grant outright - web consumers are ack-none by design, so a
  well-behaved client needs no ack grant (and no `$JS.FC.>`: ack-none push takes no
  flow control), while granting either would reopen what the deletion closed. Ack
  policy is a body field, so ack-none is a design intent the grant list cannot
  enforce - the residue is recorded below with its siblings. The lesson generalises past this
  user: **for JetStream, a grant list is a capability surface, not a read/write
  distinction** - enumerate the streams, and never hand a browser-facing user
  `$JS.API.>`.

  Residues, none of them "can read what it shouldn't", and none of them closed by the
  auth callout - `web` is browser-facing and therefore permanently statically
  authenticated, so the callout never reaches it:
  durability is a body field, so withholding the legacy `DURABLE.CREATE` subject does not
  prevent a durable - `max_consumers` per stream bounds the cost instead; ack policy is
  the same class of body field, so a hostile holder can create an explicit-ack consumer
  it holds no grant to ack - endless redeliveries, churn against the server, and an
  amplifier for the deliver-subject write below; within the four
  granted streams consumer names are the caller's choice, so `web` can pull a delivery
  off another reader's consumer or retune it through create-as-update - a route that
  reaches the gateway's relay durable from `worker` too, measured on the render: one
  permitted `$JS.API.CONSUMER.CREATE.TASKS.gateway-relay` retunes its filter subject, and
  one carrying `inactive_threshold` has the server reap it, ack floor and all, with
  `CONSUMER.DELETE` refused in the same run; and a consumer's
  deliver subject can aim replay of stored messages at another stream's subject, which is
  a persisted write under the messages' original subjects - not forgery, since a read by
  subject never sees them, but an eviction lever against the capturing stream. Delivery
  needs a subscription whose subject is **exactly** the deliver subject - a push consumer
  registers through `Sublist.registerNotification`, which takes interest only from a match
  that is byte-equal, so a wildcard subscription covering the deliver subject supplies
  none - and that splits the streams
  (measured on 2.10.29 and 2.14.5): the topic streams have literal subjects, so their own
  ingest is the interest and the write lands unaided; DIRECTORY, TASKS and the buckets
  have wildcard subjects, so reaching
  `a2a.agents.>` (the identity plane) takes a principal subscribed to a card subject
  itself. A watcher on `a2a.agents.>` - exactly `gateway`'s subscribe grant, and inside
  `web`'s `a2a.>` - is not that principal: measured, DIRECTORY stayed empty.
  This survives per-stream scoping of any user that may create consumers at all, the
  worker included; the closure is not holding `CONSUMER.CREATE`, which is a consumer
  created per task by the dispatcher. Per-name scoping is **not** available as a
  mitigation: NATS wildcards match whole tokens, so a `web-*` grant matches a consumer
  literally named `web-*` and nothing else - measured, not assumed. The real close is a
  separate account with an export/import, which stays open.

  **The probe subject.** `a2a.topics.shared.probe` is provisioned into `TOPICS-STATE`
  with **no writer in any user's publish list** - the single deliberate exception to the
  rule above that a topic's subject list and its writer's grant travel together. It
  exists so an authorization probe has a real provisioned subject to be refused on: a
  refusal against an unprovisioned subject proves only that the subject is missing, while
  a refusal here proves the grant. Aiming such a probe at a topic the fleet reads means
  that the one time the grant is wrong, the probe writes junk into standing state; a
  writerless subject makes that failure land nowhere.

  Posture, stated accurately: plain ws puts the credential on the pod network in
  cleartext, and the Service is ClusterIP, so nothing outside the cluster reaches it.
  Amended 8/31: the pod network is fenced too. The operator renders an ingress
  NetworkPolicy on the NATS pod granting **4222 to exactly the enumerated bus clients**
  (the auth callout, the agent pod - whose sidecars, the Hermes bridge included, share
  its labels - the A2A gateway, session pods by the spawner's labels, the provision Job,
  and the hand-applied seed Job), and **no pod-network peer for 8222 or 9222**. The demo's
  `kubectl port-forward` and the kubelet's readiness probe both enter from the node,
  which NetworkPolicy does not govern, so the ws surface stays reachable through
  kubectl and through nothing else in-cluster. The enumeration is today's client
  list, and it must grow with the components this spec designs. The auth callout was
  the first, and it landed in the same change that armed the callout rather than after
  it, for the reason that makes this rule worth having: the callout is itself a bus
  client sitting on the connection path, so a fence that does not name it refuses the
  one peer every new connection depends on - and nothing looks broken when that
  happens, because established connections are already authorized and keep working
  while the fabric silently accepts no new client. Still owed as they arm: the audit
  exporter, the janitor, and the metrics scrape (the alert set above is scraped
  series), and the NATS pods themselves on their route port the moment the deployment
  leaves the single-node dev shape - a 3-node cluster's servers dial each other, and a
  fence without the route peer prevents the cluster from ever forming. The topic-grant corollary that two edits
  travel together, applied to the fence. A second policy in the same amendment
  fences the session pods' egress (DNS, 4222 by label, LiteLLM - a spawned worker has
  no other legitimate destination, carrying no ServiceAccount and no Workload
  Identity; its bus credential is the static worker user, and the auth callout does not
  reach it - a session pod carries no ServiceAccount and no projected token for the
  callout to resolve, so this closes when each session gets a principal of its own
  rather than when the callout arms). The origin
  allow-list (`allowed_origins`, not `same_origin`, which can never match a UI on a
  different port) remains the browser-side control: WebSockets are exempt from CORS,
  so for as long as a port-forward runs, any page the operator's browser visits could
  otherwise drive this surface.

- **Bucket access is subject access.** KV and the Object Store ride internal subjects -
  `$KV.{bucket}.>`, `$O.{bucket}.C.>` / `$O.{bucket}.M.>`, plus the `$JS.API` surface for
  their streams - and the deny-by-default map grants them explicitly per role: the
  gateway gets `session-state`, workers get the artifact bucket, nobody gets a bucket
  their role doesn't name. Miss this and the first oversized artifact dies with an
  Authorization Violation. Within the artifact bucket, visibility is bucket-wide;
  per-task artifact scoping is parked with the per-task credentials tightening.

The callout reads an identity-to-permissions map rendered by the operator (**amended
8/24** for the subagent framework; **amended 9/4** to what ships): one entry per
callout-authenticated principal, keyed by the ServiceAccount as TokenReview spells it
(`system:serviceaccount:<namespace>:<name>`), rendered into ConfigMap
`<agent>-a2a-authmap` under key `identities.json`. Today that is one entry, the
provisioning Job, which is the only workload anything in-tree renders an `a2a-bus`
token for; the agent pod is **not** among them, because it connects as the static
shared `worker` and a map entry no token can ever match authenticates nobody. Nor is
the gateway - also a static `nats.conf` user for now - and there is no audit exporter
or janitor yet. Session pods are the next entry, and they arrive with per-session
credentials. The designed shape is one entry per `AgentProfile` rendered from the CR's
bus grants, which arrives with the CRD.
Profiles come and go at runtime, so the map cannot be a static gitops artifact; the CRs
are the declarative source and admission bounds what a profile may grant. The agents
never read the map - the constrained party does not see its own ceiling, it just hits it.
The callout reads the map through an API informer, not a volume mount: kubelet ConfigMap
sync lags up to a minute, and the dispatcher can spawn a Job seconds after a profile
lands - a race that ends in an Authorization Violation for a legitimate worker. The
ordering is enforced, not hoped for: the operator sets `BusCredentialsReady` only after
the callout reports serving, and nothing dispatches before that condition is true.
Submissions queue on the stream meanwhile; nothing is lost. The callout logs the map
version it is serving and exposes it at runtime on `/status` and `/readyz`, so "the map
says X" is checkable against the running system rather than against the rendered object.

**What ships today is coarser than that sentence, and the gap is deliberate.** The
condition is on the `PlatformAgent`, not on an `AgentProfile`, because neither the CRD
nor the dispatcher exists yet. It asserts that the callout Deployment is Available with
every replica ready - and since the readiness probe answers 503 until a map is being
served AND the replica is attached to the bus, that means every replica is serving one
and can be reached to answer with it. The bus half of that probe was added after the
first version of this paragraph: a replica holding a good map with a dead connection
answers no authorization request, and it was the one state no health signal represented,
so the Deployment stayed Available and this condition stayed true while the bus
authorized nobody. Reasons are `CalloutServing`,
`CalloutUnavailable` and `CalloutAbsent`, and the message names the rendered map version.
It does **not** confirm that a named replica has observed a named version: a sub-second
window after a re-render can report ready while a replica still serves the previous map.
That is acceptable while the identity set changes only when the operator re-renders it,
and it stops being acceptable when profiles arrive at runtime - at which point this
becomes a per-replica version check, which is exactly what the status endpoint already
exposes. Rolling the callout pods on every map change would close the gap and was
rejected: the callout is on the connection path, so a rolling restart is a window in
which new connections fail, which is the thing the informer exists to avoid.

Three numbers in the render are load-bearing and none of them is a preference.

**`max_control_line: 65536`.** A ServiceAccount token travels inside the client's CONNECT
frame, and the 4096 default bounds that whole frame - measured, the usable room for the
token is around 3920 bytes once the rest of the CONNECT JSON is counted. A token bound to
several audiences, from a client with a long name, does not fit. The failure is not
graceful: the server closes the connection with "Maximum Control Line Exceeded" _before_
authentication happens, so it reads as the bus refusing a workload rather than as a size
limit.

**`authorization.timeout: 2`.** A ceiling, not a tuning knob. The server starts a
first-ping timer on a connection that has not yet authenticated at roughly two seconds,
and the Go client aborts the connect on a PING where it required a PONG - reporting
"expected 'PONG', got 'PING'", which names nothing about authorization and sends whoever
is debugging it to the network layer. A merely _slow_ callout hits this too, so the real
budget for a TokenReview round trip is under two seconds whatever this number says.

**The token contract.** Audience `a2a-bus`, mounted at `/var/run/secrets/a2a-bus/token`,
3600s expiry, kubelet-rotated. The audience is the load-bearing part: a TokenReview that
requests no audience validates against the API server's own, which every ordinary pod's
default ServiceAccount token already carries - so without the binding the bus would accept
any readable token in the cluster as proof of that pod's identity. A long-lived client
MUST re-read the file when it reconnects rather than caching its first read, or it fails
exactly when the bus restarts, which this spec calls a routine operation.

The callout service runs in its own `AUTH` account - not `$SYS`, despite subscribing to
a `$SYS.REQ.*` subject - with 2 replicas, joined in a queue group. The queue group is
not optional above one replica: with a plain subscription every replica answers every
request and the server silently takes whichever arrives first, which makes authorization
a latency race whenever a rollout has two policy versions live at once. It is on the connection
path: if it is down, no _new_ connection succeeds, while established connections
continue. The blast radius, named honestly: during a callout outage nothing new
connects, which means no new tasks and no new workers - the fabric is dark to new work,
not gracefully degraded. Established sessions and the resilience contract below are
what make that acceptable at this stage; a hardened HA callout is production posture,
not part of the dev toggle.

**The throughput ceiling, so it is known rather than discovered.** A replica answers
authorization requests one at a time: the subscription is a queue-group subscription with
an async handler, the client library dispatches one subscription's callbacks from one
goroutine, and the handler does its TokenReview round trip inline. Two replicas is
therefore two authorizations in flight for the whole fabric, and a slow API server sets
the rate directly: two divided by the TokenReview latency, which is about 20 connections a
second at a 100ms round trip. The queue behind it is bounded by the same under-two-second
budget as everything else on this path. Measured, not read off the code, by
`TestTheCalloutAnswersOneAuthorizationAtATime` in `a2a/authcallout`, which makes the
TokenReview slow and observes that no two overlap. It is adequate for the connection rate
this stage has - connections are rare compared to messages - and it is the first thing to
look at if a fleet reconnecting at once is slow to come back. Raising it means handling
each request in its own goroutine with a bound, which is a change to make against a
measurement rather than in advance of one.

## Observability and audit

**Audit.** The retained stream is the audit substrate, not an audit log - a replayable
stream nobody can query is evidence in the same sense that a disk image is evidence. The
deployment's job is to keep the substrate intact and reachable:

- The stream is the **buffer and replay window, not the archive.** Nothing accumulates on
  NATS forever: W bounds what the bus holds, and long-term audit lives in the customer's
  log sink.
- An **audit exporter** binds a reserved durable `audit` consumer on each message stream
  and writes envelopes to the sink - Cloud Logging on GKE, pluggable elsewhere (stock
  Kubernetes stays a hard requirement). Its acks track its own progress and delete
  nothing; limits retention means cleanup is W's job, not the exporter's. Deliberately no
  purge-on-export: the window is what everyone else replays, and an exporter that deletes
  is an exporter that can destroy evidence.
- **Exporter lag has two data-loss horizons, and the alert watches both.** The age
  horizon: backlog older than W dies by retention. The byte horizon: under a flood,
  `discard: old` deletes by byte pressure _before_ age - so an operator must never be
  told W is the only thing that can delete evidence. Lag is a first-class alert on
  whichever horizon is nearer: backlog age approaching W, or stream bytes approaching
  `max_bytes`.
- The audit path is read-only by construction, and "read-only" means what the
  JetStream tax above establishes it can mean: the exporter's user publishes nothing
  onto the A2A subjects, and holds no grant that can destroy evidence - no purge, no
  stream or message delete, no consumer delete beyond its own. It does hold the
  client grants reading requires, because reading a stream is not a subscribe-only
  act: the reserved durable `audit` consumer is created through `$JS.API`, and its
  acks are publishes to `$JS.ACK`. A user rendered from a literal no-publish reading
  cannot create its consumer, and would redeliver forever if it somehow had one -
  its own lag alerts firing on a user structurally unable to make progress. Enforced
  at connect like everything else.
- The attribution salt the gateway hashes identifiers with is `SESSION_KV_SALT`, the
  per-install salt the chart already provisions into `platform-agent-secrets` for the
  shipped attribution path - not a new secret this deployment mints. Replicas must
  read that one value or the pseudonyms on the stream stop joining, to each other and
  to session metadata. The gateway design owns the rule and why a second salt is worse
  than no salt.
- W stays a tenancy decision as well as a cost one - the bus holds labelled content at rest
  for the whole window.

**Tracing.** Trace context and the correlation identifier travel in the message envelope,
next to the capability identifier - the payload spec owns those fields. What this layer
owes: the client library creates publish and consume spans carrying that context, so one
trace spans chat ingress, every hop, and whatever the hop did. The server does not
participate in traces and does not need to.

**Metrics.** Standard server metrics via the Prometheus exporter. Beyond that, per-consumer
health is a first-class signal: `num_pending`, ack-pending depth, and delivery-binding
freshness, per stream, exported. A connection can be perfectly healthy at the TCP level
while its consumer is deaf (see the next section), and the metrics have to be able to say
so.

The starting alert set, so 3 AM triage has named invariants rather than vibes (dev
defaults; the numbers are tunable, the invariants are not):

| Alert                    | Threshold                             | Severity |
| :----------------------- | :------------------------------------ | :------- |
| `AuditExporterLagAge`    | backlog age > 4h (vs W=72h)           | Critical |
| `AuditExporterLagBytes`  | stream bytes > 80% of `max_bytes`     | Critical |
| `ProfileDispatchBacklog` | > 20 pending for > 15m on one profile | Warning  |
| `ConsumerUnackedStalled` | > 0 stalled for > 10m                 | Warning  |
| `AuthCalloutErrorRate`   | > 1%                                  | Critical |

## Client resilience contract

These are **requirements on the stage 1 client library, not advice.** The motivating
incident: a client wrapper in a lab deployment mishandled a routine server restart and
spent two days silently unable to receive directed tasks, with the process up and
TCP-level health checks green. The customer restarts NATS as a routine operation,
so a client that deadlocks on restart is a support ticket generator that scales with the
number of installs.

The requirements are stated as properties. The evidence behind them is nats-py-specific;
whatever language the stage 1 library lands in, the error taxonomy maps per client library
and the property is what conformance tests.

- **NR-1: Distinguish terminal from transient.** The library MUST branch on
  terminal connection close (rebuild the client and re-subscribe) vs transient reconnect
  (wait for the underlying library, tear nothing down). Test: induce each state; assert
  the two paths are taken.
- **NR-2: Rebuild, never retry into a dead context.** On terminal close the library MUST
  construct a fresh client, re-establish JetStream, and re-subscribe to the durable. It
  MUST NOT retry subscribe or consumer-delete calls on objects bound to the dead
  connection. Test: force terminal close; assert no call is issued against the old client
  object after the state is entered.
- **NR-3: Connection callbacks registered and logged.** Closed, disconnected, reconnected
  and error callbacks MUST all be registered, and each event logged with the server error
  that triggered it. In the worked example none were registered, so the error that flipped
  the client to terminal close was never captured and the root cause is unrecoverable.
  Test: assert all four are registered at construction; assert a forced disconnect produces
  the log line.
- **NR-4: Recreate-or-bind is an explicit decision.** After reconnect the library MUST
  explicitly either bind to the stored delivery subject or recreate the consumer with a
  fresh inbox. Relying on the client library's undocumented drift behavior is forbidden.
  Test: code inspection plus a restart test asserting the chosen path executes.
- **NR-5: Consumer health beyond TCP.** The library MUST expose consumer binding state and
  pending-message depth as metrics, and the component's health check MUST incorporate them.
  "TCP is up" is not health. Test: orphan the consumer; assert the health check fails
  while TCP remains connected.
- **NR-6: Jittered backoff on every connection attempt.** The library MUST apply
  randomized exponential backoff with full jitter to connection and reconnection
  attempts. A NATS or callout restart otherwise turns every client into one synchronized
  thundering herd against the callout service and the API server behind its TokenReviews.
  Test: force a reconnect storm across N clients; assert attempt timestamps spread rather
  than align.

## Conformance

The incident's conformance assertion, carried verbatim, goes into the stage 1 library's
test suite:

> **"a NATS client survives a server restart and resumes delivery without process restart."**

Per the negative-test discipline, the test proves the property, not the wording: kill the
connection at the transport level (not a clean drain), then assert the wrapper
re-establishes, the durable consumer is re-bound and delivering within a timeout, and the
reconnect event was logged. A test that would pass against "uses nats-py" is the wrong
test.

Two deployment-side assertions belong in the same suite:

1. A connection whose user lacks publish permission on subject S is refused by the server
   at connect/publish time. The message is never readable by any consumer.
2. A message that has been delivered and acked is still replayable from the stream within
   the retention window.

Both run against `kind`.

## Open questions

- ~~**Retention window W.**~~ Decided 8/24: the placeholders are the dev defaults - 72h
  task events, 30d journals, no age limit on state. The GA number is escalated to
  product; it is a tenancy call, not ours to guess.
- ~~**Who owns the audit exporter.**~~ Decided 8/24: stage 2, landing with the parity
  suite - that is when there is evidence worth archiving. Stage 1 ships the reserved
  consumer only. Owner assigned when stage 2 staffs.
- **Which server error flips a client to terminal close** rather than transient reconnect is
  unconfirmed - the worked example never logged it. NR-3 closes this for the future and the
  uncertainty does not change NR-1 or NR-2.
- **Sizing.** TBD: message rates per stream class once the payload spec settles, and PV
  sizing from W times those rates.
