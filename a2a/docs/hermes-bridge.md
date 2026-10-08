# The Hermes bridge

- **Author:** [@bnaylor]
- **Date:** 2026-08-26
- **Status:** draft for review
- **Companions:** the A2A payload spec (task lifecycle, steering), the NATS deployment
  spec (accounts), the subagent profiles spec (supervision, CAS)

## Purpose

The retargeted first wave routes every gateway task to the addressee `platform`, and
nothing answers on that subject yet - the worker adapter (W4) fast-follows, and the
dispatcher is stage 3. The bridge is the stand-in executor: a small Go daemon on
`a2a/lib` that consumes tasks addressed to `platform`, runs each as a turn in its
conversation's Hermes session through the pod's API server (or, as the fallback, as a
`hermes -p platform chat -Q --query=<prompt>` subprocess; [executors](#executors)), and publishes
the lifecycle events with the answer as the `result` artifact and the persona's tool calls as
`activity`. It is scaffolding with a planned demolition date:
when the dispatcher and worker adapter land, the bridge retires. Nothing here is
protocol - the wire contract is the payload spec's, unchanged.

## Where it runs

**Sidecar in the platform-agent pod, rendered by the operator.** Under `mode: next` the
operator renders the bridge as a container named `hermes-bridge` beside the agent container,
and so it does under version skew (a mode this operator build does not recognize), where a
frozen bus that is still running keeps its executor. Under `today` it renders none. The
bridge needs two things that only exist in that pod: the `hermes` CLI (it lives in the
platform-agent image, so the bridge image builds FROM it and adds one static binary) and
the persona state - `$HERMES_HOME` is the agent's data PVC, RWO, holding the platform
profile's config, memory, and skills. A separate Deployment would need that PVC mounted
cross-pod, which RWO only allows with same-node scheduling games. Not worth it for a
component we intend to delete.

The rendered container is the agent container, copied: its env, `envFrom`, mounts,
`securityContext`, resources and pull policy, so it runs Hermes against the agent's profile
state on the agent's PVC, as the pod's KSA (model auth via Workload Identity for free). Two
things are taken out. The agent's own values for the names the bridge sets for itself, and
the agent's bus identity: `AGENT_SHARED_STATE_SETUP`, `NATS_URL`, `NATS_USER`,
`NATS_PASSWORD`, `BRIDGE_CONCURRENCY`, `BRIDGE_EXECUTOR`, `A2A_ACTIVITY_SECRET` and
`A2A_BUS_USER`. And the `a2a-bus-token` mount, the `agent` principal's credential, which the
bridge never holds ([Bus user and grants](#bus-user-and-grants)). Ports and probes are not
copied. On top go the bridge's own: `AGENT_SHARED_STATE_SETUP=skip`, so the image's
entrypoint runs its container-local init and execs the bridge as it does for the dashboard
container; `NATS_URL` for the `<agent>-a2a-nats` Service; `NATS_USER=bridge` and
`NATS_PASSWORD` from the `bridge-password` key of `<agent>-a2a-nats-creds`;
`BRIDGE_CONCURRENCY`; and `A2A_ACTIVITY_SECRET` from the same Secret's `bridge-activity-key`,
optional. The `api` executor needs the pod's `API_SERVER_KEY`, which the copy carries
([Executors](#executors)).

The image is `A2A_BRIDGE_IMAGE` when that is set. Unset, and when the agent container runs
the release `platform-agent` image by tag, it is that image's registry and tag with the last
path segment swapped for `hermes-bridge`: the bridge is built `FROM` the platform-agent image
of the same commit, so the two containers are one build. Otherwise - an agent image under a
custom repository name, which has no bridge published beside it, or one pinned by digest
alone, which the swap cannot carry over - it is the image the other release A2A images
resolve to, derived from the operator image the way `A2A_GATEWAY_IMAGE` and the rest are
when unset. An install that runs a custom agent image sets `A2A_BRIDGE_IMAGE`. Three
operator settings shape the rendered bridge. The operator reads them from its own
environment, as it reads `A2A_INJECT_BACKEND`; no CR field carries them.

| Operator env             | What it sets                      | Unset                                                                                                                                                                     |
| ------------------------ | --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `A2A_BRIDGE_IMAGE`       | the bridge's image                | derived as above                                                                                                                                                          |
| `A2A_BRIDGE_CONCURRENCY` | the bridge's `BRIDGE_CONCURRENCY` | 10, Hermes's own gateway pool (not the bridge's default of 2)                                                                                                             |
| `A2A_BRIDGE_EXECUTOR`    | the bridge's `BRIDGE_EXECUTOR`    | not rendered, so the bridge's shipped default decides: `api`, given the key. A value other than exactly `api` or `cli` is treated as unset, and the operator logs it once |

The TASKS consumer reserve reads the same `A2A_BRIDGE_CONCURRENCY` the bridge is given
([sizing](#sizing-against-the-eval-harness)), and the `api` executor's pod-wide hook is
rendered by the same rule as for a declared bridge ([Executors](#executors)): the operator
counts a rendered bridge exactly like a declared one.

The rendered default is 10, not the bridge's own 2, because 10 is what the Hermes gateway
runs agent turns on (its `ThreadPoolExecutor(max_workers=10)`), and the rendered bridge is that
gateway's replacement on `next`. More workers don't let two turns race on one conversation's
history. The `api` executor runs turns in one Hermes session one at a time (`sessionTurns`), and
the `cli` executor starts every task as a fresh one-shot session with no history to share. At the default `maxSessions`
of 10 the TASKS budget for 10 workers is 110, above the 64-consumer floor. A `next` install
whose TASKS stream was created at the floor, before the rendered default was 10, is refused by
its provision Job with the ways out named: delete TASKS and let provisioning recreate it, lower
`maxSessions`, or set `A2A_BRIDGE_CONCURRENCY` lower. While that `A2AProvisionFailed`
stands, the bridge is already in the agent pod at the new concurrency (the workload renders before
the refusal parks the CR), running over the undersized stream, so consumer creates can be refused
under load. That's the shape [#2043](https://github.com/gke-labs/kube-agents/issues/2043) describes. A fresh install creates TASKS at 110 from the first
render.

**It enters the pod once the bus is provisioned.** The rendered bridge is withheld from the
agent pod until the CR's `BusProvisioned` condition is `True`: before that it has no bus to
connect to and no runtime-state bucket, exits, and would hold the agent pod in
`CrashLoopBackOff` through the bring-up. The TASKS consumer reserve does not wait; it counts
the bridge from the first `next` render, so the one provisioning Job is already sized for it
and the bridge's arrival does not re-render the Job. The cost is a second roll. On a fresh
`next` install, and on a flip from `today` back to `next`, the agent pod rolls once for the
mode and again when the bridge arrives after the Job. The agent Deployment's strategy is
`Recreate` at one replica (`resolveDeploymentReplicasAndStrategy`), so each roll is a brief
agent outage: the old pod stops before the new one starts. Once `BusProvisioned` has been
`True` the bridge stays in the pod on later renders.

**It doubles the agent container's share of the pod.** The rendered container copies the
agent container's resources, so the pod carries two of them. With the defaults (requests 1
CPU and 2Gi, limits 3 CPU and 8Gi) the bridge adds another 1 CPU/2Gi of requests and 3
CPU/8Gi of limits, roughly doubling the agent pod's requests, and a node or namespace quota
sized for the `today` pod may not schedule the `next` one. `spec.deployment.resources` sizes
both containers together; no setting sizes the bridge alone.

**A CR-declared bridge wins.** A sidecar on `spec.deployment.sidecars` is a declared bridge
if it is named `hermes-bridge`, if its `env` sets `BRIDGE_CONCURRENCY`, or if it runs the
`hermes-bridge` image. The CR keeps it and the operator renders none, so an install that
already carries one does not get two bridges. Those three are the whole contract: a
hand-declared bridge outside them (a renamed image repository under another container
name, with `BRIDGE_CONCURRENCY` unset or in `envFrom`) gets a second, rendered bridge beside
it, and the two fail on the activity door's port. Name it `hermes-bridge`. An explicit
opt-out is tracked in [#2623](https://github.com/gke-labs/kube-agents/issues/2623). The `sidecars` field takes
ordinary `corev1.Container` entries, so a declared bridge's shape is CR-authored and
reconcile leaves it alone: it has to carry its own `NATS_URL` and creds, and, for the `api`
executor, `API_SERVER_KEY` and `A2A_ACTIVITY_SECRET`. The operator reads `BRIDGE_CONCURRENCY`
back out of the entry, to size the TASKS consumer reserve, and `BRIDGE_EXECUTOR`,
`API_SERVER_KEY` and `BRIDGE_ACTIVITY_LISTEN`, to decide whether the `api` executor's
pod-wide hook is rendered; it writes none of those.

**Two names it writes on a declared sidecar too.** Under the A2A surface the render writes
`POD_NAMESPACE` and `A2A_CAPABILITY_REQUIRED` onto every sidecar it emits, rendered or
declared (`a2aExecutorSidecarEnv`), but not with the same precedence.
`A2A_CAPABILITY_REQUIRED` goes to `mergeEnvVars` as the override, so a CR value for it is
discarded on every reconcile: the switch is the install's, not the sidecar author's.
`POD_NAMESPACE` goes in underneath the container's own env, as a default, so a CR that
sets it deliberately wins. Both are inputs to the capability check below rather than
deployment preferences - see "What scope the check runs at" for why the render has to
supply `POD_NAMESPACE` at all, and for what a default install did before it did.

Concurrent hermes processes under one `$HERMES_HOME` is the kanban dispatcher's existing
posture (`deploy/docker/patches/kanban_result_required.py` documents `_default_spawn`
spawning the same kind of one-shot `hermes chat -q` process), so the bridge inherits a
known-working concurrency story. Cap is 2, matching the platform profile's `concurrency` in the profiles spec.

## What a declared sidecar costs

Two properties of riding `spec.deployment.sidecars`, which a rendered bridge does not
have. Neither is fixed, for the same reason in both cases: screening a user-supplied
container means overriding user intent, and the operator does that only where it owns the
meaning of the field - the capability switch above, and the reserved volume names the
webhook refuses. `POD_NAMESPACE` is the other name the render writes, but it goes in as a
default a CR beats, so it overrides nothing.

**Flipping to `mode: today` with a declared bridge still set takes the agent down.** A
rendered bridge leaves the pod with the mode: the flip renders none, so it needs no CR edit
first. A declared one does not leave. The operator copies `spec.deployment.sidecars` into
the pod without consulting the mode, so the flip removes the NATS Service and leaves the
bridge dialling a host that no longer resolves. Confirmed live 2026-09-05: the sidecar
crash-loops, and because it shares the agent's pod the pod never reaches Ready - the whole
agent is down, not merely carrying an A2A trace. On an install that declares its own
bridge, unset `spec.deployment.sidecars` _before_ flipping to `today`. In any flip runbook
for such an install that step is a blocker, not tidiness. `hack/rollback-roundtrip.sh`
follows it: a CR with declared sidecars has the whole list unset, not only the sidecars
that look like bus clients, flipped, and the saved list declared again once `next` is back;
a CR with none, the lane's own, is only flipped.

**The webhook does not screen sidecar env, on purpose.** The `SensitiveEnvVars`
refusal applies to `spec.deployment.env` only; a sidecar's own `env` is unscreened (the
webhook checks a sidecar's `securityContext`, and checks its `volumeMounts` against the
reserved volume names, and nothing else about it). A declared bridge depends on exactly
that gap - its `NATS_URL` and credentials arrive as sidecar env. Closing it breaks that
deployment method, so it stays open as a stated trade while the bridge exists; the
bridge's demolition removes the reason.

One provenance note: the bridge image is release surface. `a2a/Dockerfile.hermes-bridge`
builds it (`FROM` the platform-agent image plus the one static binary above), the release
workflow publishes it as `hermes-bridge` beside the other first-party images, `FROM` the
platform-agent image the same run pushed under the same commit tag, and `images.json`
carries it with `A2A_BRIDGE_IMAGE` as its operator override, which the operator reads when
it renders the bridge. `deploy/docker/cloudbuild-ci.yaml` builds the presubmit's own in its
`a2a-bridge` step when `hack/ci-deploy.sh` runs under `EVAL_MODE_NEXT=1`, `FROM` the
platform-agent image that same build produced, by the tag it just pushed and never from a
registry default; the deploy then hands it to the operator as `A2A_BRIDGE_IMAGE`, with
`A2A_BRIDGE_CONCURRENCY`, for the eval install, and leaves `A2A_BRIDGE_EXECUTOR` unset, so the
eval runs the `api` executor a customer install runs (`docs/designs/eval-next-transport.md`,
"The CI flag"). Either way the sidecar and the agent
container it shares a pod with are one build. The static `bridge` bus user the next section
describes is the released mechanism, not scaffolding graduation removes: the password arrives
as sidecar env from the operator's creds Secret, and it stays a password principal for the
reason given there.

## Bus user and grants

The `…in` subject has two reader roles by design - the dispatcher for new tasks, the
executor for everything after the submission. The bridge is both, collapsed into one
process: it holds the one durable consumer on `a2a.tasks.platform.*.in` (the dispatcher
role, `durable: bridge-platform`) and handles follow-ups and cancels for tasks it is
running (the executor role). That collapse is exactly what makes it a stand-in - when
the real dispatcher arrives, the roles separate again and the bridge has nothing left to
do.

The bridge connects as the static `bridge` user, whose grants are written for this
program and nothing else: subscribe `a2a.tasks.platform.*.in`, publish
`a2a.tasks.platform.*.events`, `$KV.runtime-state.>` both ways for the in-flight
registry below, and `_INBOX.bridge.>`. Nothing wider - a bridge that can publish
submissions is a bridge that can impersonate the gateway.

Two of those grants are the capability check's: publish `a2a.cap.verify.platform` and
subscribe `a2a.cap.reply.platform.>`. Both are single subjects rather than wildcards,
and that is load-bearing twice over. The verifier reads its caller off the last token of
the subject it was asked on, so a principal granted `a2a.cap.verify.*` could name itself
anything; and any principal permitted to subscribe to a request subject may join a queue
group on it, so a wildcard in the reply space would let this user intercept other
principals' verifications rather than merely observe them. The bridge asks as `platform`
rather than as `bridge` because `platform` is the addressee the gateway writes into the
capability's `delegate` - the name it is being asked about is the routing identity, not
the connection's. `a2a/authcallout`'s conformance suite pins both halves, including that
a forged caller name is refused by the server.

**What scope the check runs at.** The bridge resolves it from `A2A_AUTHORITY_SCOPE` if
that is set, else `POD_NAMESPACE`, else the kubelet's serviceaccount namespace file. On a
rendered install only the middle rung ever fires: the operator sets no
`A2A_AUTHORITY_SCOPE` on the sidecar, and the pod is built with
`automountServiceAccountToken: false`, so there is no namespace file to read. That is why
the render supplies `POD_NAMESPACE` from the downward API. Before it did, the third rung
returned ENOENT, the scope resolved empty, and every `platform`-scoped task was refused on
a default install - fail-closed, but closed on everything. The file read stays as the last
rung for a bridge run by hand outside the operator's render, which is the only place it
can succeed.

The bridge holds no read on the `cap` bucket. It cannot resolve a capability itself, only
ask; the verifier is the only principal on the bus that may read the store. See
`docs/architecture/09-capability-envelope.md`.

The JetStream tax is not `$JS.API.>`: it is the `$JS.API` subjects the bridge emits on
TASKS and `KV_runtime-state` — stream info, consumer create, pull, direct get, and the
KV watcher's consumer delete — named one by one in the operator's
`a2aBridgeJetStreamGrants`; plus `$JS.ACK.TASKS.>`, ack scoped to the one stream this
user consumes with explicit ack (unscoped `$JS.ACK.>` is a cross-principal +TERM), and
`$JS.FC.>`. Reads go through `DIRECT.GET` and not `STREAM.MSG.GET`; the provision script
sets `--allow-direct` on every stream so nats.go picks that route, and only that route
is granted.

Note which delete is in that list and which is not: `$JS.API.CONSUMER.DELETE` is granted
for `KV_runtime-state`, for the watcher, and withheld for TASKS. The bridge calls
`lib.TasksGet` on every task it dispatches and `lib.TaskInReplay` on a task a worker is about
to spawn whose `…in` subject's newest message is neither the submission nor a cancel (the
cancel look-ahead below; the newest-message read itself is a direct get and opens nothing),
and a call that finds messages creates an ordered consumer on TASKS; nothing deletes it. It is reaped by the five-second inactive threshold
both reads set on it, which is why the replay costs a consumer slot for the calls of the last
five seconds rather than for the last five minutes of them (gke-labs/kube-agents#1739) without
the bridge needing a destructive verb on TASKS. The slot outlives the call it served: the
threshold runs from the call returning, not from it starting. A call on a task the retention window no longer holds
creates no consumer at all -- the horizon read finds nothing before the consumer is
created. Either way the call emits no refused publish of its own. One does arrive if the
ordered consumer resets mid-replay -- a bus reconnect is enough -- because nats.go deletes
the consumer it replaces: that publish on `$JS.API.CONSUMER.DELETE.TASKS.<name>` is refused,
which costs a log line and leaves the consumer it could not delete to the same threshold.
It is the one violation this grant produces by design, so any other `Permissions Violation`
in the bridge's log still means what it says.

**Static is the answer here, not a residue.** `bridge` replaced the shared `worker` user
rather than inheriting it, and it stays a password principal on purpose. The auth
callout keys its map on the username TokenReview returns, which names a ServiceAccount;
a sidecar shares its pod's ServiceAccount, so a projected token would resolve the bridge
to the same map entry as the `agent` principal in the container beside it and hand each
of them the union of the two grant sets — which is the `worker` user rebuilt under a new
name. The callout cannot see which container opened a connection, and `Narrowing` is
pod-scoped, so no map shape available today separates them. The bridge gets a token when
it stops sharing a pod with the agent, which is the same event that retires it.

What the split bought, measured from this side: the bridge holds no grant on
`TOPICS-STATE` or `TOPICS-JOURNAL` at all — not the reads and not the writes. The
blackboard belongs to the `a2a` CLI in the agent container, which is now its own callout
principal. `TestBridgeJetStreamGrantOnARealServer`'s refused table is where that is
measured.

### Migrating an existing sidecar

An install whose `spec.deployment.sidecars` entry still names the retired user fails
closed rather than quietly: `worker` is gone from the rendered `nats.conf`, so the
sidecar's connect is refused at authentication and the container crash-loops. Because it
shares the agent's pod, the pod does not reach Ready — the same failure shape the
`mode: today` flip produces above. Two edits, both in the sidecar's own `env`:
`NATS_USER` becomes `bridge`, and `NATS_PASSWORD`'s `secretKeyRef.key` becomes
`bridge-password`. The Secret is the same `<agent>-a2a-nats-creds`; the operator fills the
new key on the next reconcile. It does not remove the old one: `ensureA2ACredsSecret` only
fills keys that are missing or empty and never prunes, so `worker-password` stays in the
Secret of an upgraded install indefinitely. It is dead data rather than a live credential —
`worker` is no longer a user in the rendered `nats.conf`, so presenting that password
authenticates to nothing — but the key's presence is not evidence the sidecar has been
migrated, and a reader checking whether an install has taken the split should read
`nats.conf` or the sidecar's `env`, not the Secret's key set.

Both edits are in `env`, and that is the supported route on purpose. A `sidecarVolumes`
entry that mounts `<agent>-a2a-nats-creds` — or the `<agent>-a2a-nats-config` or
`<agent>-a2a-callout-keys` Secret, or a projection of the `a2a-bus` audience under any
name — is refused at admission, and stripped from the render on an install running the
A2A surface, because a volume hands a second container far more than the `bridge`
principal's one password. `<agent>-a2a-nats-creds` and the `nats.conf` in
`<agent>-a2a-nats-config` both carry `sys-password`, which is the `$SYS` account, and
`<agent>-a2a-callout-keys` holds the issuer seed the auth callout signs with. The
agent's own credential is in none of them: under the callout the `agent` principal has
no shared secret at all, and the `a2a-bus` audience projection is the only route to it
as a credential. The seed is a way to mint one, which is the other reason that Secret is
not something to hand a sidecar.

A third edit is owed only by an install that overrode `BRIDGE_PROFILE`, and its failure
lands in an unhelpful place. The retired `worker` user's subscribe grant was
`a2a.tasks.*.*.in` — the addressee position was a wildcard, so pointing the bridge at
another addressee just worked. `bridge`'s grants name `platform` literally
(`a2aBridgeAddressee`, which is also `defaultProfile` in the bridge's own `main.go`: one
value living in two modules that cannot import each other). Override the env now and the
intake half still works — the consumer is created and pulled over `$JS.API`, where the
filter subject rides in the request body and no subject grant sees it — so the other
addressee's task is delivered. It stops there. `accept` publishes `submitted` on
`a2a.tasks.<other>.*.events` before it puts anything on the worker queue, and that subject
is not in the publish list, so the publish is refused, the submission is dropped, and
Hermes is never spawned. The refusal does not read as one: a rejected JetStream publish is
a reply that never arrives, so the bridge logs a timeout and the submitter waits on a task
that got no terminal event and was never run. Leave the env unset, or widen the grant in
the operator to match — and add the new addressee to `a2aReservedAddressees()` in the same
change, so the auth callout's `A2A_RESERVED_ADDRESSEES` refuses a narrowed pod named after it.
The three have to move together: a grant widened without the reservation lets a session pod
named after the second addressee read and publish its task subjects.

The agent container is the other half of the same change and needs no edit: the operator
stops rendering `NATS_USER`/`NATS_PASSWORD` there and mounts a projected token instead.
One user-visible consequence — topic entries the `a2a` CLI writes now carry
`from.session` of `agent` rather than `worker`, so a query matching on the old value
returns nothing for entries written after the upgrade.

## Executors

`BRIDGE_EXECUTOR` picks how a task runs. The daemon defaults to `api`; `cli` is the
subprocess executor the bridge started with, kept as the fallback.

**`api`: a turn in the conversation's session.** The bridge POSTs the task's text to the
Hermes API server in the same pod (`BRIDGE_API_URL`, default
`http://127.0.0.1:8642/v1/chat/completions`, model `BRIDGE_API_MODEL`, default
`model-default`) with `Authorization: Bearer $API_SERVER_KEY` and three headers:
`X-Hermes-Session-Key` and `X-Hermes-Session-Id`, both set to the session id below, and
`Idempotency-Key`, set to the task id so a redelivered task does not run its turn twice (a
follow-up turn's key adds its envelope id; see **Steering** below). The
server loads the session's history from its own store before the turn and appends to it after,
so the second task in a thread sees the first. The session id is `a2a-` plus the task's
`contextId`, which the gateway mints once per backend conversation. A `contextId` that is not
letters, digits, `_` and `-`, or is longer than 128 characters, is replaced by `a2a-h-` and 32
hex characters of its SHA-256, so an odd one still maps to one session and never to a path. Every
task has a `contextId`: the envelope refuses one without.

The turn runs under the API server's profile, the gateway's own (`default` on a stock install,
the Planning Agent) that answers the same message on the chat path and delegates through kanban, not under `BRIDGE_PROFILE`. Two
tasks in one session are serialized: the second waits for the first's turn to end, as a second
chat message waits in a chat platform's session. Tasks in different sessions run side by side
up to `BRIDGE_CONCURRENCY`, and a task waiting for its session's turn holds a worker and stays
`submitted` until its turn starts.

The sidecar starts with the agent container, so the bridge can be consuming before the API
server listens. A refused connection is retried every second for two minutes; it never reached
the server, so the retry cannot run a turn twice. The server ignores the session headers
without `API_SERVER_KEY`, so the executor needs it. The rendered bridge copies the agent
container's env, which carries it; a hand-declared sidecar must set it, and
`A2A_ACTIVITY_SECRET` from the `bridge-activity-key` entry of the a2a creds Secret for the tool
trace. With `BRIDGE_EXECUTOR` unset and no key, the bridge logs a warning and runs the `cli`
executor, so a sidecar declared before `api` existed keeps working; `BRIDGE_EXECUTOR=api` with
no key is refused at start.

What the `api` executor does not do. A kanban card the persona creates completes after the turn
has answered, and the API server has no channel to push that completion back, so it never reaches
the A2A thread; the `cli` executor loses it the same way. A turn the bridge stops waiting
for, on cancel or the deadline, may keep running in the server, and the next task on the same
session can start beside it; so can a turn Hermes starts on its own, such as a background wake.
Tool calls from either can land in the wrong task's trace. And when Hermes compresses a long
session it continues it under a new session id, which the hook reports and the trace's key does
not match, so the trace stops for that conversation while the answers keep arriving.

**`cli`: a subprocess per task.** `hermes -p <BRIDGE_PROFILE> chat -Q --query=<prompt>`, a fresh
session for every task, with no memory of the thread's earlier tasks. The prompt is one
`--query=` token, so a message that starts with `-` stays the query rather than reading as an
option, with any NUL byte dropped, since no argument can carry one. It cannot continue a
session, so a follow-up to one of its tasks is refused `no-resume` when it arrives: follow-ups run
on the `api` executor only ("Steering" below). The rest of this page describes it where the two
differ.

## Lifecycle, steering, cancel

Per task: `submitted` on accept (before the consumer ack, so a bridge death before the
ack just redelivers), `working` when the subprocess spawns, the persona's tool calls as
an `activity` artifact and a heartbeat as a `progress` artifact while it runs ("Activity"
below), the stdout as a `result` artifact (chunked if large), one terminal
`status-update` with `final: true`. On the `api` executor, when follow-ups ran as further turns
("Steering" below), each earlier turn's answer is a `turn` artifact of its own
(`artifact-<taskId>-turn-<N>`), published just before the next turn starts, and the `result` is
the last turn's answer. A nonzero exit is terminal `failed` with the evidence in the status
message: `reason: hermes-exited-nonzero - exit status N; session: <id>; stdout tail: …; stderr
tail: …`. Both tails are bounded (2 KiB each),
and `session:` carries the id when `hermes chat -Q` printed its `session_id:` line on stderr (it
is the last thing the CLI writes before exiting), so the transcript under the profile's session
store can be found from the terminal alone. Exit 75 is Hermes's `EX_TEMPFAIL` for a turn that
gave up on the provider's rate limit or billing; it is named `reason: hermes-rate-limited` instead,
which the eval harness classes as infrastructure rather than the persona's failure (the image patch
`apply_quiet_rate_limit_exit.py` makes a plain `-Q` run exit 75 on that failure, as a kanban worker
already did). A
submission with no text parts is terminal `rejected`. New-task detection is the
dispatcher's rule, and 9/9 widened it: BOTH event subjects empty means new, not `…events`
alone (profiles spec). The bridge satisfies that without a change of its own, because it
asks `lib.TasksGet` rather than reading a subject - and `tasks/get` folds `…events` and
`…supervisor` together, so a platform task carrying a supervisor terminal and nothing on
`…events` comes back `final` and is acked with a warning, not run again. That matters here
because the gateway's supervisor grant is an addressee wildcard, so such a task is
constructible. The component that does NOT get this for free is the worker adapter, whose
`priorEvents` deliberately replaced `lib.TasksGet` with a consumer on its own `…events`
(a session's grants reach neither `STREAM.INFO` nor a get-by-subject) and so cannot see a
terminal its own predecessor's supervisor declared. Anything on `…in` for a task with a
terminal event is acked with a warning and nothing else.

**Steering:** on the `api` executor a follow-up message to a running task is queued, not
refused. Follow-ups run on the `api` executor only: the `cli` executor cannot continue a session.
The bridge answers each follow-up with a non-final status carrying the task's current state
(`submitted` while queued or waiting for the session, `working` after) and a `steerNotice` data
part: `queued`, or `refused` with `no-resume` (the task runs on the `cli` executor, which can't
continue a session; every follow-up to it is refused so when it arrives, the notice's text says
to send it again after the answer, and the gateway posts that to the room at once),
`queue-full` (a task takes at most 16 follow-ups, counted per task: those already run count, not
only those waiting), `task-ending` (the answer was already
chosen), `no-text` (no text part holds anything but white space, U+001C-U+001F or NUL: Hermes's API
server refuses a turn that Python's `str.strip()` empties, which strips U+001C-U+001F too, and a
NUL alone asks nothing), `capability` (the task's capability, carried on the follow-up, did not pass
when checked on the worker before its turn: refused, or the verifier could not be reached; the one
token covers both), or `task-ended` (the task ended first: cancel, failure, deadline, shutdown).
When the current turn ends, queued follow-ups run in arrival order as further turns in the same
Hermes session: the bridge posts another turn with the same session headers, under the same session
slot, and the `Idempotency-Key` `<taskId>/<envelopeId>` (the opening turn's is `<taskId>`), so a
follow-up never replays the opening answer. A follow-up's text needs no length check of its own: the
gateway's doors cap a text at 65,536 runes, the server's own cap on a message
(`MAX_NORMALIZED_TEXT_LENGTH`, 65,536 characters), and the bus's 1 MiB message limit keeps the
request far under the server's 10 MB body limit. Each earlier turn's answer is published as a
`turn` artifact as soon as the next turn is about to run; the last turn's answer is the `result`,
then the one terminal. A turn's answer the bridge holds between turns, while the next follow-up's
capability is checked, is not lost to a shutdown: the worker ends the task with it as the `result`,
and the queue is refused `task-ended`. A failed follow-up turn names itself in the terminal
(`; turn: N` after the session). A follow-up does not change task state (payload spec assertion
12). A bridge that crashes with follow-ups queued loses them; the gateway's relay reports them as
not run at the terminal, unless the gateway restarted too. The count is best-effort: a follow-up
whose turn had started when the bridge crashed counts as run, though its answer never arrives.
Mid-turn steering through the runs API is gke-labs#2628.

**Upgrade order for steering.** The gateway and the bridge do not roll together. The gateway's
image follows the operator, but the sidecar's image is whatever the CR names, so until someone
edits the CR the two can be a release apart, and a rollback produces the reverse skew. Upgrade the
operator (and with it the gateway) first, then bump the CR's `hermes-bridge` sidecar tag. On a
rollback, move the sidecar tag back first, then the operator. The two skews look like this:

- **Old gateway, new bridge (the order to avoid).** The old relay has no case for `turn`
  artifacts, so every earlier turn's answer is dropped. The room gets the old "does not take
  mid-task input" ack, the notice's text part as "ℹ️ follow-up queued …", and then only the last
  follow-up's answer. The answer to the original question never posts.
- **New gateway, old bridge.** Noisy, but nothing is lost. The room gets "✏️ got it, I'll take
  that next", which is wrong, then the old bridge's "ℹ️ steering received but not absorbed …",
  and the task's one answer. The gateway posts its "N follow-up(s) arrived as the task finished"
  line only after it has heard a steer notice from that addressee since it started, and an old
  bridge sends none. That memory outlives the bridge, though: until the gateway restarts, a
  gateway that heard notices from that addressee before the rollback can still post one false
  "follow-up(s) arrived as the task finished" line at the end of a task that had a follow-up.

**Cancel:** SIGTERM to the subprocess's process group, SIGKILL after a grace period,
then terminal `canceled` (`reason: canceled-by-request`). A task racing to completion may
land `completed` first - both orders are legal and the terminal event wins. A per-task
deadline (default 7200s, matching the profile's `activeDeadlineSeconds`), which covers every
turn of the task, takes the same kill path and lands `failed` (`reason: deadline-exceeded - killed
after …`; on `api`, `request ended after …`, with `; turn: N` for a follow-up turn); no follow-up
turn starts once it has passed.

A cancel for a task still queued finalizes it `canceled` with `reason: canceled-before-start`
and nothing is spawned, and the worker looks for one itself before it spawns. The durable
delivers serially and acks after the handler, so a cancel already on the task's `…in`
subject when the bridge binds - the eval harness abandoning a submission nobody took, or any
cancel inside the stream's retention window - is dispatched only after the submission's
accept returns, and by then an idle worker, which a freshly bound bridge has, holds the run.
Between dequeue and spawn the worker therefore reads the task's `…in` subject for a `cancel`
newer than the submission it holds and, finding one, finalizes `canceled-before-start` and
spawns nothing. The read is the subject's newest message by direct get (`lib.LastEnvelope`,
no consumer): a `cancel` there is newer than the submission, and the submission there means
nothing followed it; only a subject whose newest message is something else, a follow-up
behind a cancel, is replayed in full (`lib.TaskInReplay`, the one-subject form of the read
`tasks/get` does on the event subjects, on the same five-second ephemeral). That is what
keeps a bind over a backlog of abandoned submissions from opening a consumer per task at bus
speed, and the replay that remains is paced: at most `BRIDGE_CONCURRENCY` of them are in hand
at once, each held until its ephemeral's five-second threshold has run after it returned, so the
look-ahead holds that many live consumer slots at most, plus whatever the server has not yet
reaped at the window's edge. The operator's reserve counts this look-ahead row, and the asks
row beside it, per worker at the `BRIDGE_CONCURRENCY` the bridge is given (the operator's
`A2A_BRIDGE_CONCURRENCY` for a rendered bridge, the sidecar's own env entry for a declared one), read
at render time, with the bridge's default of 2 standing in for an entry that is absent or that
the render cannot read as a count (a `valueFrom`, or a reference to one); a sidecar started with
a higher value, as the eval's is, widens the reserve with it, and a value the stream's floor
cannot hold at the CR's `maxSessions` is refused at provision, with the CR `Degraded` and both
inputs named, rather than left to outgrow the reserve. Twice `BRIDGE_CONCURRENCY` is the bound
the bridge's own test holds the stream to. A run the durable's cancel has already
ended takes no slot at all. `working` is published only after that read, so a cancelled
run never shows it. It is
a read, not a consume: the durable still delivers the cancel to the handler afterwards, and
it does nothing - the run is normally gone from the bridge's table by then, so the cancel
takes the orphan path, which reads the newest event the same consumer-free way, finds the
terminal and acks with a warning like any other in-traffic for a finished task; in the window
before finalize drops the run it finds it final and returns, finalize being idempotent. A read
that fails, a bus error or its 10s bound, is logged and the run spawns
anyway; the cancel still arrives on the durable and kills it, the bound the bridge always
had, where a read failure that dropped the task would leave it open with no terminal event.

Under the `api` executor `working` is published when the task's turn starts, and cancel,
shutdown and the deadline end the HTTP request, the read of the response body included, where
the `cli` executor kills a process group, with the same terminals: `canceled-by-request`, `bridge-shutdown`, `deadline-exceeded`. Ending
the request ends the bridge's wait; whether the server abandons the turn it was running is the
server's. Turns on one session run one at a time; a task still waiting for the previous turn
when its deadline passes ends `reason: session-busy - waited <d> for the session's previous
turn; no request was sent`, and one cancelled while waiting ends `canceled-before-start`. The
failures name themselves in the status message:

| Reason                   | When                                                                                                                              | Eval harness class |
| ------------------------ | --------------------------------------------------------------------------------------------------------------------------------- | ------------------ |
| `hermes-rate-limited`    | the server answered 429 (its concurrent-run cap), or `X-Hermes-Failure-Reason` names the provider's rate limit or billing         | infrastructure     |
| `hermes-api-unreachable` | no response: still refused after the two-minute retry, the task's deadline passed before any connection, or the connection failed | infrastructure     |
| `hermes-api-refused`     | any other 4xx answer: the server refused the request before running a turn                                                        | infrastructure     |
| `session-busy`           | the deadline passed while the task waited for the session's previous turn; no request was sent                                    | infrastructure     |
| `hermes-api-failed`      | a 5xx answer, or a 200 whose turn Hermes marks failed; the message carries the status, session and the error or a body tail       | persona            |
| `hermes-api-unreadable`  | a 2xx answer that is not a chat completion with at least one choice                                                               | persona            |
| `hermes-api-read-failed` | the response body broke off mid-read                                                                                              | persona            |
| `hermes-api-oversize`    | a 2xx body over the 8 MiB read cap (`apiResponseCap` in `api.go`); refused rather than truncated, with the limit named            | persona            |
| `request-encode-failed`  | the bridge could not encode the request; a bridge fault, not expected in practice                                                 | graded (unlisted)  |
| `request-build-failed`   | the bridge could not build the request; the URL is checked at start, so likewise                                                  | graded (unlisted)  |

## Sizing against the eval harness

This section is the canonical statement of the sizing; the eval transport design
([`eval-next-transport.md`](../../docs/designs/eval-next-transport.md), stage 1) summarises it.
An eval install runs the bridge against the presubmit's fan-out, and
two numbers bound what it can take. `BRIDGE_CONCURRENCY` (default 2, the cap above) is the worker
count: a task past it is accepted and `submitted` and then queued with no subprocess until a
worker frees, and the harness classifies a repetition that reaches its budget still
`submitted` as infrastructure rather than a graded case - it cancels the task and the bridge
answers `canceled-before-start`. The presubmit fans units out at `EVAL_TASK_PARALLELISM`,
default 4, the nightly at 8, so the install gives the bridge a `BRIDGE_CONCURRENCY` at or above
that value; at the defaults two of every four concurrent units wait for as long as the two ahead
of them run. The queue behind the workers is fixed at `taskQueueCapacity`, 1024 in `bridge.go`,
and a submission past it is finalized `failed` with `reason: bridge-queue-overflow`, also
infrastructure in the harness's classification. Size the parallelism against both: concurrency
at or above the parallelism, and the number of submissions a run can have outstanding at once,
the units in flight plus anything abandoned and not yet cancelled, well under the queue
capacity. `hack/ci-deploy.sh` under `EVAL_MODE_NEXT=1` sets the operator's
`A2A_BRIDGE_CONCURRENCY` to the run's `EVAL_TASK_PARALLELISM`, and the operator renders that
into the bridge. It sizes the TASKS consumer reserve from the same value, so a concurrency the
stream cannot hold is a refused provision and a `Degraded` CR, not a silent shortfall. That
read, the render in [Where it runs](#where-it-runs), the two env names it writes on every
sidecar and the `api` executor's hook entry are the operator code the bridge has, and they
retire with the bridge.

## Activity: the persona's tool calls, and a heartbeat

Under `-Q` hermes writes nothing to stdout until the final response, so the pipe the
bridge holds says nothing about tool calls while the run is on. What hermes does offer is
its outbound webhooks: a `hooks.outbound` entry in the profile's config POSTs every
`pre_tool_call` and `post_tool_call` to a URL, fire-and-forget through a bounded queue,
HMAC-SHA256 signed when the variable its `secret_env` names is set. The bridge listens
for those on a loopback address in the pod (`BRIDGE_ACTIVITY_LISTEN`, default
`127.0.0.1:8651`, clear of hermes's API server on 8642 and the agent-api-auth
listener on 8643; `off` closes the door) and hands each child the entry through hermes's
managed scope: before spawning, it writes a per-task directory holding the operator's
managed `config.yaml` with a `hooks.outbound` entry added (URL the door actually bound,
`secret_env: A2A_ACTIVITY_SECRET`, appended to any entry the operator's own managed config
carries; a `hooks` or `hooks.outbound` of another shape fails the spawn like an unreadable file, and
the copy keeps the operator's numbers as written) and the managed `.env` verbatim, and names it in the child's `HERMES_MANAGED_DIR`
(the source is `$HERMES_MANAGED_DIR` as the sidecar sees it, else `/etc/hermes` when it
exists; `BRIDGE_SCRATCH_DIR` is where the copies live: made if absent while the door is open, and
with the door closed a scratch dir that cannot be made or read is logged and the bridge starts,
since nothing is written there; each copy removed when
its child exits, the ones a previous incarnation left (its direct subdirectories that carry the
bridge's own marker file, whatever their name, nothing else in it and never the directory
itself) removed when the bridge starts, and
none written at all when the source exists but cannot be read, since a child on a hook-only
scope would run without the operator's pins). On a sidecar declaring `BRIDGE_EXECUTOR=cli`
the hook therefore exists only in processes the bridge spawned: a kanban worker or cron
tick under the same profile never POSTs anywhere, a pod with no bridge has nothing to POST at,
and nothing about the profile's shipped config or the image changes for it. The `api`
executor needs a pod-wide entry instead, described at the end of this section.

Under `cli`, nothing in a delivery names the A2A task: hermes's own `task_id` is the kanban card or a
fresh UUID, `cwd` and `profile` are shared by every process under the profile, and the URL
does not expand environment variables. So the bridge gives each child a random key in its
environment under `A2A_ACTIVITY_SECRET` (and the door's URL under `A2A_ACTIVITY_URL`, for
the record; hermes reads the URL from its config), and a delivery belongs to whichever in-flight
task's key verifies its signature — at most `BRIDGE_CONCURRENCY` keys to try. Unsigned or
unmatched deliveries are answered 204 and dropped.

What goes on the bus, one `data` part per invocation at `post_tool_call`, a superset of the
worker adapter's `{"tool","input"}` so one fold reads both executors:

```json
{
  "tool": "mcp__gke__list_clusters",
  "input": { "project": "p" },
  "callId": "call_01",
  "status": "completed",
  "durationMs": 2130,
  "at": "2026-09-25T20:01:02Z"
}
```

`status` is `completed` or `error` from the hook's own verdict, and on `error` an
`errorType` keeps hermes's word for it (`blocked`, `cancelled`, `timeout`, `tool_error`),
so a guardrail refusal stays distinguishable from a tool failure. hermes retries a delivery
that timed out, so each is remembered by its id (the most recent 8192 per task) and a retry is one call. A call still open when the
task finalizes — deadline, cancel, a crash mid-tool, or a `post_tool_call` the door could not
read (logged, not counted, since the call is then in the trace; an unreadable `pre_tool_call`
costs nothing, its `post` carries the record whole) — is flushed as `interrupted` inside
the finalize lock, ahead of the result and the terminal, so the trace is complete and
nothing of it follows the final event. What of the input is published is its **shape**: its
structure with every string value replaced by `<string, N chars>` and every key that is not
shaped like a schema key (a letter or `_` first, then letters, digits, `_`, `-`, at most 48, and not token-like: no credential prefix, no long digit, hex or single-case run, few changes of character class) by
`<key n, N chars>`, numbers, booleans and the redaction markers kept, and one exception, the nested tool names of hermes's `tool_call`
wrapper (`calls[].name` at the wrapper's own level), which is all the graders read (a tool's
name, a wrapper's nested names). No grammar tells a resource name from a credential under the
same key, so none is attempted, no string value rides the trace under any key, and there is no
value scrub to have a reach: a credential a model pastes into a terminal command is one string
under `command` and ships as a length. Values under keys with `token`, `secret`, `password`,
`passwd`, `authorization`, `passphrase`, `api_key`/`api-key`, `private_key`, `ssh_key`,
`signing_key`, `key_data`, `cookie` or `credential` as a whole component (`access_token`,
`SECRET_KEY`, `accessToken`, `clientSecret`, `SecretAccessKey`, `secretAccessKey`, `PGPASSWORD`,
`client-key-data`, `Cookie`; not `tokenizer`, and not a key that opens with the word and goes on
as a name, reference or location, `secretName`, `tokenPath`) are replaced by `[redacted]` before
the shaping, so the trace still says a secret-looking key was there. The door reads a delivery of at most 8 MiB (hermes carries the tool input and result whole, so a
large file write is a few MiB); a larger one is refused and logged, and since the cut body cannot
be verified nothing counts it, the one loss the `activity-budget` marker does not see. The
over-size delivery is normally the call's `post_tool_call`, the one carrying the result, whose
small `pre_tool_call` arrived and opened the call: that call then ends `interrupted` at the
terminal although the tool finished. An over-size `pre_tool_call` leaves the call absent. `input` is capped (2 KiB): over the cap it becomes
`{"truncated": true, "bytes": N, "head": "..."}`, except for hermes's `tool_call` wrapper,
where each nested call's `arguments` is capped on its own so the nested tool names stay
readable. Tool results are not published: no check
reads them and they are the riskiest payload in the pod. One task publishes at most 3000
trace and progress parts together: the task's events subject is capped at 4096 messages, and
a looping persona publishing without bound would evict its own `submitted` and `working`. The
two have shares of their own: the heartbeat 120 (one a minute for the default two-hour
deadline), the trace the rest, so the heartbeat keeps going on exactly the run that spends the
trace's share, and an interval short enough to spend the heartbeat's share silences the
heartbeat and never the trace. Past the trace's share, calls are counted and one entry, `tool` `activity-budget` with `status`
`truncated` and `dropped` saying how many, goes out at the terminal ahead of the result. The stream keeps every activity
part; the relay drops the artifact on purpose (debug and audit views never render to
chat), and the inject door's probe is where a reader sees it.

The heartbeat is a `progress` text part every `BRIDGE_PROGRESS_INTERVAL_SECONDS` (default
60; 0 turns it off, and a count a duration cannot hold is refused with a log line and the
default): `running 1m30s, 3 tool call(s), last mcp__gke__list_clusters`. With the door closed,
or under `api` with no `A2A_ACTIVITY_SECRET` to check a delivery against, the count has nothing
to move it, so the line says so instead: `running 1m30s, tool trace off`.
The relay renders progress as one edited rolling line, so this is one chat edit per minute,
and it is what tells a stuck task from a slow one from outside the pod.

Under the `api` executor there is no child to hand a key or a scope to: the turn runs in
the long-lived gateway process, which serves the chat platforms, cron and kanban dispatch too.
So the operator renders the entry in the pod's managed config instead, when the CR renders
the agent's A2A surface (`mode: next`, or a mode this operator build does not recognize) and
renders a bridge, or declares a bridge sidecar (a sidecar whose env sets `BRIDGE_CONCURRENCY`), that runs `api` and
whose door the entry reaches. It runs `api` by the bridge's own rule: `BRIDGE_EXECUTOR=api`, or
that entry empty and `API_SERVER_KEY` non-blank or taken from a reference. The door is reached
when `BRIDGE_ACTIVITY_LISTEN` is unset or names the hook's port on `127.0.0.1` or a wildcard
host. With several bridge sidecars, every one must qualify; a value the operator cannot read
(`BRIDGE_EXECUTOR` or `BRIDGE_ACTIVITY_LISTEN` through `valueFrom`) disqualifies. Then
`hooks.outbound` gains `a2a-bridge-activity`, posting `pre_tool_call` and `post_tool_call` to
`http://127.0.0.1:8651/hermes/tool-events`, signed with `A2A_ACTIVITY_SECRET`, read from the
`bridge-activity-key` entry of the a2a creds Secret. The operator puts it in the agent
container's env; the sidecar needs it in its own. A `cli` bridge, including one that fell back
to `cli` for want of a key, gets no entry and no key, and so does a bridge sidecar the operator
cannot see, one that leaves `BRIDGE_CONCURRENCY` unset or takes it through `envFrom`. An `api`
bridge with the door open and no key logs a warning at start, and so does one whose door is not
where the hook posts; either reports `tool trace off`. Every hermes
process in the pod now posts to the door, and a delivery's `session_id` is what attributes it:
it counts for the task whose session id matches and which holds that session's turn at the
moment, and is dropped otherwise, so a kanban worker's or a chat message's tool calls, which
carry their own session ids, never reach an A2A trace. Kanban work the turn delegates is
therefore absent from the trace; only the gateway profile's own calls appear. The `cli`
executor still uses a per-task key, and a child it spawns drops the pod-wide entry from the
managed config it is given, so a delivery is never signed twice. The managed scope merges per
leaf and a list is one leaf, so the rendered `hooks.outbound` replaces any list a profile sets
in its own `config.yaml`; no profile the product ships sets one, and a profile that needs its own
outbound hooks does not get them while the entry is rendered.

Trust boundary, stated: everything in the pod is reachable from the persona's own terminal
tool, its environment included. The shared key widens that under `api`: any process holding
the agent's environment can sign a delivery naming another conversation's session id while
that session has a turn in flight, and so add entries to that task's trace. The trace is
informational, read by the eval harness and by debug views, and nothing authorizes on it.
The trace is "as reported by the executor's process", the
worker adapter's posture too; the key rejects cross-talk, not adversaries.

## Supervision

The bridge finalizes its own orphans, which is not the same as being their supervisor.
The ratified split says every task's supervisor is the component that spawned its
execution; the bridge spawned its own execution, so there is no separate supervisor to
be - and no supervisor WRITE either.

**It is not a supervisor principal, and 9/9's subject split does not move it.** The
gateway spawns session pods and finalizes tasks it did not execute, so it publishes on
`…supervisor`, a subject no executor reaches. The bridge is the executor. When it
finalizes an orphan it is finishing its OWN task across a restart, as itself, so its
terminal stays on `…events` where every other event it writes goes, and its `from`
agrees with that subject like any executor's. Profile-addressed tasks have no
supervisor ROLE until the dispatcher lands - though the gateway's rendered grant is an
addressee wildcard and reaches their `…supervisor` regardless, which the profiles spec
spells out; that is a gap the profiles
spec names, not one this sweep fills. Two failure classes:

- **The subprocess dies under a live bridge.** The runner sees the exit and publishes
  terminal `failed` with the evidence. Ordinary executor path, nothing special.
- **The bridge dies mid-task.** The submission was already acked, so nobody redelivers
  it, and no terminal event exists. The bridge keeps an in-flight registry in the
  `runtime-state` KV bucket (key per accepted taskId, written before `submitted`,
  deleted after the terminal publish). On startup, before consuming, it sweeps: fold
  each registered task's events, and for any non-final one publish terminal `failed`
  (`reason: bridge-died-without-terminal-event`).

The sweep's publish is a compare-and-swap per the profiles spec, not a read-then-write:
expected-last-subject-sequence pinned to the last event the fold saw. A dying
subprocess's flush racing the sweep wins cleanly, the CAS is rejected, and the sweep
re-reads instead of double-finalizing. Whichever writer loses lands in the
warn-and-drop path like any other post-final event.

That still holds here after 9/9, and it is worth saying why, because the profiles spec
records that the same CAS stopped protecting the dispatcher's janitor on that date.
Expected-last-subject-sequence is per subject. The janitor's terminal moved to
`…supervisor` while the executor kept writing `…events`, so the two writers stopped
sharing the subject the CAS is evaluated on. The bridge's sweep reads and writes the
same `…events` subject as the runner it is racing, so the racing write still
invalidates the expectation and the server still refuses the loser.

The sweep assumes incarnations are serial. That assumption is real on this install -
the kubelet restarts the sidecar container in place, and the operator renders the agent
Deployment with strategy `Recreate`, so two bridges never run at once. It is an
assumption, not a mechanism, and it has a named boundary: `Recreate` is the render's
default only while replicas is 1 - `spec.deployment.availability.replicas` above 1
switches the strategy to RollingUpdate (`resolveDeploymentReplicasAndStrategy`,
`manifest_helpers.go`), and an overlapping incarnation could then sweep-fail a task
its predecessor is still running. Real executor fencing belongs to the stage-3
dispatcher, not to scaffolding with a demolition date.

Honest gaps, accepted for the playground: no queue-staleness guard (the lib's subscribe
path doesn't expose server ingest timestamps, and `queueTimeoutSeconds` is the
dispatcher's job when it exists), no heartbeats on `agents.hb.>` (the `progress` heartbeat
above is on the task's own subject, for its readers, not a liveness signal for a
supervisor), a submission whose
events lookup keeps failing is dropped with a log line rather than redelivered (a new
submission's lookup is answered by the direct horizon gets and opens no consumer, so it gets
one quick retry for a bus hiccup; an orphan cancel's lookup opens the consumer and can be
refused at the TASKS cap, so a failure from before the consumer existed is retried over six
seconds, past the inactive threshold the refusal clears on, and a failure from after it is not,
since that consumer is live and another attempt would open another; the lib acks
unconditionally after the handler, so a lookup a shutdown interrupts is also dropped, and a nak
path is a lib delta if a persistent failure ever bites),
and a terminal publish that fails outright - a bus outage outlasting the finalize
budget at exactly that moment - leaves the task in the registry for the NEXT
incarnation's sweep, which may be far away on a healthy sidecar; until then the bridge
holds the task as done while the stream shows no terminal event, and a later cancel
cannot unstick it.
All retire with the bridge. One more: zombie reaping. The operator deliberately leaves
`ShareProcessNamespace` unset (`platformagent_manifests.go`, the pod-template comment)
so no container hands its `/proc/<pid>/environ` to its neighbours. The bridge binary is
therefore PID 1 of its own container, orphans escaping the group kill reparent to it,
and Go's `cmd.Wait` reaps only direct children - zombies can accumulate until the
container restarts. Accepted for the playground with the rest; the stage-3 dispatcher
runs executions as Jobs, where the problem does not exist.

## Definition of done

A task published to `a2a.tasks.platform.<id>.in` on the W6 install returns the platform
agent's real answer as a `result` artifact. Cancel works. The event sequence passes
the lifecycle conformance assertions (9, 10, 12, 13, 14, 15, 18), table-driven like the
`a2a/lib` conformance suite. Two of those changed meaning on 9/9 without changing number:
9 gained the supervisor-only-terminal exception and 10 now spans both event subjects, and
the bridge's own tests still assert the single-subject form. They pass because a
platform task has no supervisor writing to it in practice, not because they cover the
new wording - so do not read a green bridge suite as coverage of the split.
